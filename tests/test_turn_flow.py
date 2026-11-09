"""Both desktop entry points use Gemini audio directly, without a separate speech stack."""

import asyncio
import os
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import types
from pipecat.frames.frames import (
    InputAudioRawFrame, InterruptionFrame, LLMContextFrame, TTSAudioRawFrame, TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

from scripts import inference_tts
from src.agent import conversation
from src.agent.services import build_llm_service
from src.integrations.mcp import MCPRuntime
from tests.test_mcp_pipeline import FakeGemini


class Passthrough(FrameProcessor):
    def __init__(self, output):
        super().__init__()
        self.output = output

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.output.append(frame)
        await self.push_frame(frame, direction)


class StalledOutput(BaseOutputTransport):
    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True))
        self.speaking = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.rendered = asyncio.Event()

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        self.speaking.set()
        await self.rendered.wait()
        return True

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            self.interrupted.set()
            self.rendered.set()
        await super().process_frame(frame, direction)


class TurnFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_entry_point(self, typed):
        runtime = MCPRuntime([])
        self.addAsyncCleanup(runtime.close)
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(runtime, defer_connection=not typed)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)

        async def text(session, value):
            await session.say("A response from Gemini.")

        fake = FakeGemini(on_text=text)
        connect = Mock(wraps=fake.connect)
        llm._client.aio.live.connect = connect
        frames = []
        transport = SimpleNamespace(
            input=lambda: Passthrough(frames), output=lambda: Passthrough(frames)
        )
        commands, events = queue.Queue(), queue.Queue()
        if typed:
            with (
                patch.object(
                    inference_tts, "AppleAudioTransport", return_value=transport
                ),
                patch.object(inference_tts, "build_llm_service", return_value=llm),
            ):
                running = asyncio.create_task(
                    inference_tts._run_pipeline(commands, events, runtime)
                )
                try:
                    await asyncio.wait_for(llm.ready.wait(), 2)
                    commands.put("Hello.")
                    async with asyncio.timeout(2):
                        while not any(isinstance(f, TTSAudioRawFrame) for f in frames):
                            await asyncio.sleep(0.01)
                    texts = []
                    while not events.empty():
                        texts.append(events.get())
                    self.assertIn(("text", "A response from Gemini."), texts)
                    self.assertIn(
                        {"role": "user", "content": "Hello."},
                        llm.context.get_messages(),
                    )
                finally:
                    commands.put(None)
                    await asyncio.wait_for(running, 3)
        else:
            with (
                patch.object(conversation, "build_llm_service", return_value=llm),
                patch.object(conversation, "SherpaDetector", return_value=SimpleNamespace(
                    process=lambda pcm, rate: ["Luna"],
                )),
            ):
                worker = conversation.build_worker(transport, runtime)
            started = asyncio.Event()

            @worker.event_handler("on_pipeline_started")
            async def on_started(worker, frame):
                started.set()

            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                await asyncio.wait_for(started.wait(), 2)
                connect.assert_not_called()
                self.assertFalse(llm.ready.is_set())
                await worker.queue_frame(InputAudioRawFrame(b"\x01\0" * 320, 16000, 1))
                await asyncio.wait_for(llm.ready.wait(), 2)
                async with asyncio.timeout(2):
                    while not fake.sessions[0].audio:
                        await asyncio.sleep(0.01)
                self.assertEqual(
                    fake.sessions[0].audio[0].mime_type, "audio/pcm;rate=16000"
                )
                self.assertEqual(fake.sessions[0].audio[0].data, b"\x01\0" * 320)
                connect.assert_called_once()
                await fake.sessions[0].say("A response from Gemini.")
                async with asyncio.timeout(2):
                    while not any(isinstance(f, TTSAudioRawFrame) for f in frames):
                        await asyncio.sleep(0.01)
            finally:
                await worker.cancel()
                await asyncio.wait_for(running, 3)
        self.assertFalse(any(isinstance(f, TTSSpeakFrame) for f in frames))
        self.assertTrue(
            all(
                f.sample_rate == 24000
                for f in frames
                if isinstance(f, TTSAudioRawFrame)
            )
        )

    async def test_voice_streams_pcm_in_and_gemini_audio_out(self):
        await self.run_entry_point(typed=False)

    async def test_typed_input_uses_the_same_live_audio_service(self):
        await self.run_entry_point(typed=True)

    async def test_end_conversation_disconnects_and_allows_repeated_wakes(self):
        activities = []
        runtime = MCPRuntime([])
        self.addAsyncCleanup(runtime.close)
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
            llm = build_llm_service(runtime, defer_connection=True)
        self.addAsyncCleanup(llm._client.aio.aclose)
        self.addCleanup(llm._client.close)
        fake = FakeGemini()
        llm._client.aio.live.connect = fake.connect
        wake, ambient = b"\x01\0" * 320, b"\x02\0" * 320
        detector = SimpleNamespace(
            process=Mock(side_effect=lambda pcm, rate: ["Luna"] if pcm == wake else []),
            reset=Mock(),
        )
        frames = []
        output = StalledOutput()
        transport = SimpleNamespace(
            input=lambda: Passthrough(frames), output=lambda: output,
        )
        with (
            patch.object(conversation, "build_llm_service", return_value=llm),
            patch.object(conversation, "SherpaDetector", return_value=detector),
        ):
            worker = conversation.build_worker(transport, runtime, on_activity=activities.append)
        started, ended = asyncio.Event(), asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def on_started(worker, frame):
            started.set()

        @llm.event_handler("on_conversation_ended")
        async def on_ended(service):
            ended.set()

        async def wait_for(predicate):
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0.001)

        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            for cycle in range(2):
                ended.clear()
                output.speaking.clear()
                output.interrupted.clear()
                output.rendered.clear()
                await worker.queue_frame(InputAudioRawFrame(wake, 16000, 1))
                await asyncio.wait_for(llm.ready.wait(), 2)
                await wait_for(lambda: fake.sessions[-1].audio)
                session = fake.sessions[-1]
                self.assertIn("end_conversation", session.functions)
                description = session.functions["end_conversation"].description
                self.assertIn("closing acknowledgements", description)
                self.assertIn("nevermind", description)
                self.assertIn("thanks, now turn it down", description)
                instruction = session.config.system_instruction
                self.assertIn("without waiting for an explicit stop-listening command", str(instruction))
                self.assertEqual(session.audio[-1].data, wake if cycle == 0 else ambient + wake)
                await llm._handle_context(llm.context)
                self.assertFalse(session.responses)

                if cycle == 0:
                    runtime.catalog_revision += 1
                    await worker.queue_frame(InputAudioRawFrame(ambient, 16000, 1))
                    await wait_for(lambda: len(fake.sessions) == 2 and fake.sessions[-1].audio)
                    self.assertTrue(session.closed)
                    session = fake.sessions[-1]
                    self.assertIn("end_conversation", session.functions)
                    self.assertEqual(session.functions["end_conversation"].description, description)
                    self.assertEqual(session.config.system_instruction, instruction)

                if cycle == 0:
                    await session.say("An earlier response.")
                    await asyncio.wait_for(output.speaking.wait(), 2)
                llm._session_resumption_handle = "old-session"
                await session.call("end_conversation")
                await wait_for(lambda: llm._end_requested)
                self.assertFalse(session.closed)
                self.assertFalse(output.interrupted.is_set())
                if cycle == 0:
                    output.rendered.set()
                await wait_for(lambda: session.responses)
                self.assertEqual(session.responses[-1].response, {"status": "ending_after_reply"})
                await session.messages.put(types.LiveServerMessage(
                    server_content=types.LiveServerContent(generation_complete=True),
                ))
                await asyncio.sleep(0.02)
                self.assertIsNone(llm._end_task)
                self.assertFalse(session.closed)

                output.speaking.clear()
                output.rendered.clear()
                await session.say("No problem.")
                await asyncio.wait_for(output.speaking.wait(), 2)
                await wait_for(lambda: llm._end_task is not None)
                self.assertFalse(session.closed)
                self.assertFalse(ended.is_set())
                self.assertTrue(activities[-1])
                self.assertFalse(output.interrupted.is_set())
                output.rendered.set()
                await asyncio.wait_for(ended.wait(), 2)
                self.assertTrue(session.closed)
                self.assertFalse(llm.ready.is_set())
                self.assertIsNone(llm._session_resumption_handle)
                self.assertFalse(running.done())
                self.assertEqual(detector.reset.call_count, cycle + 1)
                self.assertTrue(output.interrupted.is_set())
                self.assertFalse(activities[-1])
                await wait_for(
                    lambda: sum(
                        m.get("content") == '{"status": "ending_after_reply"}'
                        for m in llm.context.get_messages() if isinstance(m, dict)
                    ) == cycle + 1
                )

                session_count, audio_count = len(fake.sessions), len(session.audio)
                response_count = len(session.responses)
                detection_count = detector.process.call_count
                if cycle == 1:
                    runtime.catalog_revision += 1
                await worker.queue_frame(LLMContextFrame(llm.context))
                await worker.queue_frame(InputAudioRawFrame(ambient, 16000, 1))
                await wait_for(lambda: detector.process.call_count > detection_count)
                await llm._tool_result("late", "read_value", {"value": 42})
                await asyncio.sleep(0.02)
                self.assertEqual(len(fake.sessions), session_count)
                self.assertEqual(len(session.audio), audio_count)
                self.assertEqual(len(session.responses), response_count)
            self.assertEqual(len(fake.sessions), 3)
            self.assertEqual(activities, [True, False, True, False])
        finally:
            await worker.cancel()
            await asyncio.wait_for(running, 3)
