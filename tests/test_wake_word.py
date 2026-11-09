"""Local detection and the audio/connection boundary; no live microphone or Gemini."""

import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from pipecat.frames.frames import EndFrame, InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner

from scripts import setup_wake_word
from src.audio.wake_word import MODEL_DIR, MODEL_FILES, SherpaDetector, WakeWordGate


class Collector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.audio = bytearray()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio.extend(frame.audio)
        await self.push_frame(frame, direction)


class WakeWordTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, detector, activate):
        gate, sink = WakeWordGate(detector, activate), Collector()
        worker = PipelineWorker(
            Pipeline([gate, sink]), params=PipelineParams(audio_in_sample_rate=16000),
            enable_rtvi=False, idle_timeout_secs=None,
        )
        errors, started = [], asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def on_started(worker, frame):
            started.set()

        @worker.event_handler("on_pipeline_error")
        async def on_error(worker, frame):
            errors.append(frame.error)
            await worker.cancel()

        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())

        async def stop():
            await worker.cancel()
            await asyncio.wait_for(running, 3)

        self.addAsyncCleanup(stop)
        await asyncio.wait_for(started.wait(), 2)
        return gate, sink, worker, running, errors

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    async def test_wake_once_preserves_preroll_and_speech_during_connection_in_order(self):
        connecting, ready = asyncio.Event(), asyncio.Event()

        async def activate():
            connecting.set()
            await ready.wait()

        detector = SimpleNamespace(process=Mock(side_effect=[[], ["Luna"]]))
        gate, sink, worker, _, errors = await self.start(detector, activate)
        ambient, wake, command, followup = (bytes([n, 0]) * size for n, size in
                                           ((1, 20000), (2, 320), (3, 320), (4, 320)))
        await worker.queue_frame(InputAudioRawFrame(ambient, 16000, 1))
        await self.wait_for(lambda: detector.process.call_count == 1)
        self.assertEqual(len(gate._buffer), 32000)
        self.assertEqual(sink.audio, b"")
        await worker.queue_frame(InputAudioRawFrame(wake, 16000, 1))
        await asyncio.wait_for(connecting.wait(), 2)
        await worker.queue_frame(InputAudioRawFrame(command, 16000, 1))
        await self.wait_for(lambda: len(gate._buffer) == 32000 + len(command))
        self.assertEqual(sink.audio, b"")
        ready.set()
        await self.wait_for(lambda: len(sink.audio) == 32000 + len(command))
        await worker.queue_frame(InputAudioRawFrame(followup, 16000, 1))
        await self.wait_for(lambda: len(sink.audio) == 32000 + len(command) + len(followup))
        self.assertEqual(sink.audio, (ambient + wake)[-32000:] + command + followup)
        self.assertEqual(detector.process.call_count, 2)
        self.assertEqual(gate._buffer, b"")
        self.assertEqual(errors, [])

    async def test_stop_during_connection_cancels_activation_and_discards_audio(self):
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                connecting, cancelled = asyncio.Event(), asyncio.Event()

                async def activate():
                    connecting.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                gate, sink, worker, running, errors = await self.start(
                    SimpleNamespace(process=lambda pcm, rate: ["Luna"]), activate
                )
                await worker.queue_frame(InputAudioRawFrame(b"\x01\0" * 320, 16000, 1))
                await asyncio.wait_for(connecting.wait(), 2)
                if graceful:
                    await worker.queue_frame(EndFrame())
                else:
                    await worker.cancel()
                await asyncio.wait_for(running, 2)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(gate._buffer, b"")
                self.assertEqual(sink.audio, b"")
                self.assertEqual(errors, [])

    async def test_errors_and_connection_buffer_limit_stop_without_forwarding_audio(self):
        for failure in ("detector", "connection", "format", "buffer"):
            with self.subTest(failure=failure):
                connecting = asyncio.Event()

                async def activate():
                    connecting.set()
                    if failure == "connection":
                        raise RuntimeError("connection failed")
                    await asyncio.Event().wait()

                detector = SimpleNamespace(process=Mock(return_value=["Luna"]))
                if failure == "detector":
                    detector.process.side_effect = RuntimeError("detector failed")
                gate, sink, worker, running, errors = await self.start(detector, activate)
                await worker.queue_frame(InputAudioRawFrame(
                    b"\x01\0" * 320, 16000, 2 if failure == "format" else 1
                ))
                if failure == "buffer":
                    await asyncio.wait_for(connecting.wait(), 2)
                    await worker.queue_frame(InputAudioRawFrame(b"\0\0" * 520000, 16000, 1))
                await asyncio.wait_for(running, 2)
                self.assertTrue(errors)
                self.assertEqual(gate._buffer, b"")
                self.assertEqual(sink.audio, b"")

    async def test_shutdown_before_wake_never_activates(self):
        activate = AsyncMock()
        _, sink, worker, running, errors = await self.start(
            SimpleNamespace(process=lambda pcm, rate: []), activate
        )
        await worker.cancel()
        await asyncio.wait_for(running, 2)
        activate.assert_not_awaited()
        self.assertEqual(sink.audio, b"")
        self.assertEqual(errors, [])

    async def test_rearm_discards_old_audio_and_cannot_revive_a_stopped_gate(self):
        detector = SimpleNamespace(process=lambda pcm, rate: [], reset=Mock())
        gate, sink, worker, running, errors = await self.start(detector, AsyncMock())
        await worker.queue_frame(InputAudioRawFrame(b"\x01\0" * 320, 16000, 1))
        await self.wait_for(lambda: gate._buffer)
        await gate.rearm()
        detector.reset.assert_called_once()
        self.assertEqual(gate._buffer, b"")
        self.assertEqual(gate._state, "waiting")
        await worker.cancel()
        await asyncio.wait_for(running, 2)
        await gate.rearm()
        self.assertEqual(gate._state, "stopped")
        detector.reset.assert_called_once()
        self.assertEqual(sink.audio, b"")
        self.assertEqual(errors, [])

    async def test_rearm_failure_stops_the_pipeline(self):
        gate, _, _, running, errors = await self.start(
            SimpleNamespace(
                process=lambda pcm, rate: [],
                reset=Mock(side_effect=RuntimeError("reset failed")),
            ),
            AsyncMock(),
        )
        await gate.rearm()
        await asyncio.wait_for(running, 2)
        self.assertEqual(gate._state, "stopped")
        self.assertIn("reset failed", errors[0])

    async def test_shutdown_during_rearm_cannot_restart_detection(self):
        detector = SimpleNamespace(process=lambda pcm, rate: [], reset=Mock())
        gate, _, worker, running, _ = await self.start(detector, AsyncMock())
        resetting, release = asyncio.Event(), asyncio.Event()

        async def reset(function):
            resetting.set()
            await release.wait()
            function()

        with patch("src.audio.wake_word.asyncio.to_thread", side_effect=reset):
            rearming = asyncio.create_task(gate.rearm())
            await asyncio.wait_for(resetting.wait(), 2)
            await worker.cancel()
            await asyncio.wait_for(running, 2)
            release.set()
            await asyncio.wait_for(rearming, 2)
        self.assertEqual(gate._state, "stopped")


class DetectorTests(unittest.TestCase):
    def test_invalid_settings_and_corrupt_download_fail_without_native_startup(self):
        for threshold in (0, -1, 2, float("nan"), float("inf")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                SherpaDetector(threshold=threshold)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                SherpaDetector(model_dir=folder)
            target = Path(folder) / "models" / "model"
            with (
                patch.object(setup_wake_word, "MODEL_DIR", target),
                patch.object(setup_wake_word, "urlopen", return_value=BytesIO(b"bad archive")),
                self.assertRaisesRegex(RuntimeError, "checksum mismatch"),
            ):
                setup_wake_word.download_model()
            self.assertEqual(list(target.parent.iterdir()), [])

    @unittest.skipUnless(
        all((MODEL_DIR / name).is_file() for name in MODEL_FILES)
        and (MODEL_DIR / "test_wavs" / "en_0.wav").is_file(),
        "Requires the local wake word model from scripts.setup_wake_word",
    )
    def test_real_model_handles_silence_and_repeated_upstream_keyword(self):
        detector = SherpaDetector()
        self.assertEqual(detector.phones, "L UW1 N AH0")
        self.assertEqual(detector.process(b"\0\0" * 16000, 16000), [])
        for audio, rate in ((b"\0", 16000), (b"\0\0", 48000)):
            with self.assertRaises(ValueError):
                detector.process(audio, rate)
        detector = SherpaDetector("LIGHT UP")
        found = []
        for attempt in range(3):
            if attempt == 2:
                detector.reset()
            with wave.open(str(MODEL_DIR / "test_wavs" / "en_0.wav")) as clip:
                while pcm := clip.readframes(320):
                    found.extend(detector.process(pcm, clip.getframerate()))
            for _ in range(50):
                found.extend(detector.process(b"\0\0" * 320, 16000))
        self.assertEqual(found, ["LIGHT UP"] * 3)


if __name__ == "__main__":
    unittest.main()
