"""Timing logs follow native service boundaries without logging generated content."""

import asyncio
import unittest
from unittest.mock import patch

from loguru import logger
from pipecat.frames.frames import ErrorFrame, LLMContextFrame, TTSAudioRawFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.tts import OpenAITTSService

from src.agent.services import build_llm_service, build_tts_service


class ServiceTimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        sink = logger.add(lambda message: self.logs.append(message.record["message"]),
                          filter=lambda record: record["name"] == "src.agent.services", level="INFO")
        self.addCleanup(logger.remove, sink)

    async def test_native_llm_boundaries_first_text_and_multiple_requests(self):
        llm = build_llm_service()
        self.addAsyncCleanup(llm._client.close)

        async def response(context):
            await llm._push_llm_text("private reply")
            await llm._push_llm_text("more private text")

        with patch.object(llm, "_process_context", response), \
                patch("src.agent.services.perf_counter", side_effect=[10, 10.25, 11.5, 20, 20.1, 21]):
            for _ in range(2):
                await llm.process_frame(LLMContextFrame(LLMContext()), FrameDirection.DOWNSTREAM)
        self.assertEqual(self.logs, [
            "LLM inference #1 started", "LLM inference #1 first text after 0.250s",
            "LLM inference #1 ended: 1.500s (completed)", "LLM inference #2 started",
            "LLM inference #2 first text after 0.100s", "LLM inference #2 ended: 1.000s (completed)",
        ])

    async def test_llm_failure_is_not_reported_as_completed(self):
        llm = build_llm_service()
        self.addAsyncCleanup(llm._client.close)

        async def response(context):
            await llm.push_frame(ErrorFrame("test failure"))

        with patch.object(llm, "_process_context", response):
            await llm.process_frame(LLMContextFrame(LLMContext()), FrameDirection.DOWNSTREAM)
        self.assertEqual(len(self.logs), 2)
        self.assertTrue(self.logs[-1].endswith("(failed)"))

    async def test_tts_frames_timing_and_generator_cleanup(self):
        tts = build_tts_service()
        self.addAsyncCleanup(tts._client.close)
        frames = [TTSAudioRawFrame(b"\0\0" * 100, 24000, 1), TTSAudioRawFrame(b"\0\0" * 100, 24000, 1)]
        closed = []

        async def speech(service, text, context_id):
            try:
                for frame in frames:
                    yield frame
            finally:
                closed.append(context_id)

        with patch.object(OpenAITTSService, "run_tts", speech):
            with patch("src.agent.services.perf_counter", side_effect=[10, 10.4, 10.6]):
                output = [frame async for frame in tts.run_tts("secret", "one")]
            self.assertEqual(output, frames)
            self.assertEqual(self.logs, [
                "TTS synthesis [one] started (6 characters)",
                "TTS synthesis [one] first audio after 0.400s",
                "TTS synthesis [one] ended: 0.600s (completed)",
            ])
            generator = tts.run_tts("secret", "two")
            self.assertIs(await anext(generator), frames[0])
            await generator.aclose()
        self.assertEqual(closed, ["one", "two"])
        self.assertTrue(self.logs[-1].endswith("(cancelled)"))
        self.assertFalse(any("secret" in message for message in self.logs))

    async def test_tts_error_and_task_cancellation(self):
        tts = build_tts_service()
        self.addAsyncCleanup(tts._client.close)

        async def error(service, text, context_id):
            yield ErrorFrame("test failure")

        with patch.object(OpenAITTSService, "run_tts", error):
            output = [frame async for frame in tts.run_tts("secret", "error")]
        self.assertIsInstance(output[0], ErrorFrame)
        self.assertTrue(self.logs[-1].endswith("(failed)"))

        entered, closed = asyncio.Event(), asyncio.Event()

        async def wait(service, text, context_id):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield None
            finally:
                closed.set()

        with patch.object(OpenAITTSService, "run_tts", wait):
            task = asyncio.create_task(anext(tts.run_tts("secret", "cancel")))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertTrue(self.logs[-1].endswith("(cancelled)"))


if __name__ == "__main__":
    unittest.main()
