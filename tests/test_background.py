"""Background worker rebuilding and cancellation without native device access."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.agent import conversation
from src.agent.services import build_llm_service
from src.integrations.mcp import MCPRuntime
from tests.test_mcp_pipeline import FakeGemini
from tests.test_turn_flow import Passthrough


class FakeAudio:
    def __init__(self):
        self.source, self.sink = Passthrough([]), Passthrough([])
        self._close = Mock()

    def input(self):
        return self.source

    def output(self):
        return self.sink


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_audio_worker_rebuilds_and_shutdown_closes_the_replacement(self):
        runtime, stopped = MCPRuntime([]), asyncio.Event()
        self.addAsyncCleanup(runtime.close)
        original_build = conversation.build_worker
        workers, transports, services = [], [], []

        def audio():
            result = FakeAudio()
            transports.append(result)
            return result

        def service(integrations, **kwargs):
            result = build_llm_service(integrations, **kwargs)
            result._client.aio.live.connect = FakeGemini().connect
            services.append(result)
            return result

        def build(transport, integrations, memory=None, *, on_activity=None, on_audio=None):
            self.assertIs(integrations, runtime)
            worker = original_build(transport, integrations, memory=memory,
                                    on_activity=on_activity, on_audio=on_audio)
            workers.append(worker)

            @worker.event_handler('on_pipeline_started')
            async def on_started(worker, frame):
                if len(workers) == 1:
                    await transport.input().push_error('Simulated sleep/wake audio failure',
                                                       force_treat_as_permanent=True)
                else:
                    stopped.set()

            return worker

        with (
            patch.dict(os.environ, {'GOOGLE_API_KEY': 'test-key'}),
            patch.object(conversation, 'AppleAudioTransport', side_effect=audio),
            patch.object(conversation, 'SherpaDetector', return_value=SimpleNamespace(
                process=lambda pcm, rate: [], reset=Mock(),
            )),
            patch.object(conversation, 'build_llm_service', side_effect=service),
            patch.object(conversation, 'build_worker', side_effect=build),
        ):
            await asyncio.wait_for(conversation._run_background(runtime, stopped), 3)
        self.assertEqual(len(workers), 2)
        for transport in transports:
            transport._close.assert_called_once()
        self.assertTrue(all(not llm.ready.is_set() for llm in services))

    async def test_stop_during_hardware_retry_does_not_restart(self):
        stopped = asyncio.Event()
        with patch.object(conversation, 'AppleAudioTransport', side_effect=RuntimeError('No device')) as audio:
            running = asyncio.create_task(conversation._run_background(MCPRuntime([]), stopped))
            async with asyncio.timeout(2):
                while not audio.called:
                    await asyncio.sleep(0.001)
            stopped.set()
            await asyncio.wait_for(running, 1)
            audio.assert_called_once()

    async def test_missing_configuration_fails_without_repeated_native_startup(self):
        transport = FakeAudio()
        with (
            patch.object(conversation, 'AppleAudioTransport', return_value=transport) as audio,
            patch.object(conversation, 'build_worker', side_effect=FileNotFoundError('No wake model')),
            self.assertRaises(FileNotFoundError),
        ):
            await conversation._run_background(MCPRuntime([]), asyncio.Event())
        audio.assert_called_once()
        transport._close.assert_called_once()
