"""Amplitude, native audio notifications, and the active voice/UI boundary."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from pipecat.frames.frames import BotStoppedSpeakingFrame, InputAudioRawFrame, InterruptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.workers.runner import WorkerRunner

from src.agent import conversation
from src.audio.levels import pcm_levels
from src.integrations.mcp import MCPRuntime
from tests import test_apple_audio
from tests.test_mcp_pipeline import FakeGemini
from tests.test_turn_flow import Passthrough


class LevelTests(unittest.TestCase):
    def test_envelopes_follow_pcm_amplitude_with_no_integer_overflow(self):
        self.assertEqual(pcm_levels(b""), (0,) * 5)
        self.assertEqual(pcm_levels(bytes(640)), (0,) * 5)
        samples = np.repeat([0, 128, 512, 2048, 8192], 64).astype("<i2").tobytes()
        levels = pcm_levels(samples)
        self.assertEqual(list(levels), sorted(set(levels)))
        self.assertEqual(pcm_levels(b"\xff\x7f" * 320), (1,) * 5)
        self.assertEqual(pcm_levels(b"\0\x80" * 320), (1,) * 5)
        self.assertEqual(pcm_levels(b"\0\0"), (0,) * 5)


class NativeLevelTests(unittest.IsolatedAsyncioTestCase):
    make_audio = test_apple_audio.AppleAudioTests.make_audio

    async def test_capture_notification_uses_the_same_echo_cancelled_pcm_as_the_agent(self):
        audio, _, _ = self.make_audio()
        source = audio.input()
        source._capturing = True
        source._sample_rate = 16000
        pcm = b"\0\x10" * 320
        source._resampler.resample = AsyncMock(return_value=pcm)
        source.push_audio_frame = AsyncMock()
        captured = []

        @source.event_handler("on_audio_captured")
        async def on_audio(transport, frame):
            captured.append(frame)

        await source._push_audio(bytes(1920), 48000)
        self.assertEqual(captured[0].audio, pcm)
        self.assertIs(source.push_audio_frame.call_args.args[0], captured[0])
        source._capturing = False
        await source._push_audio(bytes(1920), 48000)
        self.assertEqual(len(captured), 1)

    async def test_stopped_notification_waits_for_render_or_immediate_interruption(self):
        audio, _, _ = self.make_audio()
        output = audio.output()
        stops = []

        @output.event_handler("on_playback_stopped")
        async def on_stop(transport):
            stops.append(True)

        rendered = asyncio.Event()
        output._wait_for_playback = AsyncMock(side_effect=rendered.wait)
        with patch.object(BaseOutputTransport, "push_frame", new_callable=AsyncMock):
            stopping = asyncio.create_task(output.push_frame(BotStoppedSpeakingFrame()))
            await asyncio.sleep(0)
            self.assertEqual(stops, [])
            rendered.set()
            await stopping
            self.assertEqual(stops, [True])
        with patch.object(BaseOutputTransport, "process_frame", new_callable=AsyncMock):
            await output.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(stops, [True, True])


class MeteredTransport(Passthrough):
    def __init__(self):
        super().__init__([])
        for name in ("on_audio_captured", "on_audio_delivered", "on_playback_stopped"):
            self._register_event_handler(name, sync=True)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InputAudioRawFrame):
            await self._call_event_handler("on_audio_captured", frame)
        await super().process_frame(frame, direction)


class VoiceLevelFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_active_capture_and_delivered_playback_reach_the_ui(self):
        runtime = MCPRuntime([])
        self.addAsyncCleanup(runtime.close)
        source, output = MeteredTransport(), MeteredTransport()
        transport = SimpleNamespace(input=lambda: source, output=lambda: output)
        events = []
        wake = b"\x01\0" * 320
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = conversation.build_llm_service(runtime, defer_connection=True)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        fake = FakeGemini()
        llm._client.aio.live.connect = fake.connect
        with (
            patch.object(conversation, "build_llm_service", return_value=llm),
            patch.object(conversation, "SherpaDetector", return_value=SimpleNamespace(
                process=lambda pcm, rate: ["Luna"] if pcm == wake else [], reset=Mock(),
            )),
        ):
            worker = conversation.build_worker(transport, runtime, on_audio=events.append)
        started = asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def on_started(worker, frame):
            started.set()

        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())

        async def wait_for(predicate):
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(started.wait(), 2)
            frame = InputAudioRawFrame(b"\0\x10" * 320, 16000, 1)
            await source._call_event_handler("on_audio_captured", frame)
            self.assertEqual(events, [])
            await worker.queue_frame(InputAudioRawFrame(wake, 16000, 1))
            await asyncio.wait_for(llm.ready.wait(), 2)
            await source._call_event_handler("on_audio_captured", frame)
            self.assertEqual(events[-1][:2], ("user", pcm_levels(frame.audio)))
            count = len(events)
            await fake.sessions[-1].say("A reply.")
            await wait_for(lambda: any(isinstance(f, TTSAudioRawFrame) for f in output.output))
            self.assertEqual(len(events), count)
            played = TTSAudioRawFrame(b"\0\x20" * 480, 24000, 1)
            await output._call_event_handler("on_audio_delivered", played)
            self.assertEqual(events[-1][:2], ("luna", pcm_levels(played.audio)))
            await output._call_event_handler("on_playback_stopped")
            self.assertEqual(events[-1][:2], ("luna", None))
            await llm._end_conversation()
            count = len(events)
            await source._call_event_handler("on_audio_captured", frame)
            self.assertEqual(len(events), count)
        finally:
            await worker.cancel()
            await asyncio.wait_for(running, 3)
