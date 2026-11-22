"""Local wake word detection and the microphone gate before Gemini connects."""

import asyncio
from pathlib import Path
import tempfile

import numpy as np
from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
import sherpa_onnx

from src.paths import HOME


MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
MODEL_DIR = HOME / "models" / MODEL_NAME
MODEL_FILES = (
    "encoder-epoch-13-avg-2-chunk-8-left-64.onnx",
    "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
    "joiner-epoch-13-avg-2-chunk-8-left-64.onnx",
    "tokens.txt",
    "en.phone",
)
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL_NAME}.tar.bz2"
MODEL_SHA256 = "68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6"


def model_ready(model_dir=MODEL_DIR):
    return all((model_dir / name).is_file() for name in MODEL_FILES)


def download_model(progress=None, *, model_dir=MODEL_DIR, url=MODEL_URL, sha256=MODEL_SHA256):
    """Fetch the pinned model once into `model_dir`. `progress(done, total)` follows the download (total may be 0)."""
    import hashlib
    import tarfile
    from urllib.request import urlopen

    if model_dir.exists():
        if not model_ready(model_dir):
            raise RuntimeError(f"Incomplete model directory: {model_dir}")
        return
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=model_dir.parent) as staging:
        archive = Path(staging) / "model.tar.bz2"
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, archive.open("wb") as output:
            total, done = int(getattr(response, "headers", {}).get("Content-Length") or 0), 0
            while chunk := response.read(256 * 1024):
                digest.update(chunk)
                output.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if digest.hexdigest() != sha256:
            raise RuntimeError("Model checksum mismatch; download discarded.")
        with tarfile.open(archive) as bundle:
            bundle.extractall(staging, filter="data")
        extracted = Path(staging) / model_dir.name
        if not model_ready(extracted):
            raise RuntimeError("The downloaded model is missing required files.")
        extracted.rename(model_dir)


class SherpaDetector:
    def __init__(self, phrase="Luna", threshold=0.25, *, model_dir=MODEL_DIR):
        if not 0 < threshold <= 1:
            raise ValueError("Threshold must be greater than 0 and at most 1.")
        model_dir = Path(model_dir)
        if not all((model_dir / name).is_file() for name in MODEL_FILES):
            raise FileNotFoundError(
                "Wake word model files are missing. Run uv run python -m scripts.setup_wake_word."
            )
        lexicon = {}
        for line in (model_dir / "en.phone").read_text(encoding="utf-8").splitlines():
            word, *phones = line.split()
            lexicon[word] = phones
        words = phrase.upper().split()
        if not words:
            raise ValueError("The wake phrase cannot be empty.")
        missing = [word for word in words if word not in lexicon]
        if missing:
            raise ValueError(f"No English pronunciation in this model for: {', '.join(missing)}")
        phones = [phone for word in words for phone in lexicon[word]]
        tokens = {line.split()[0] for line in (model_dir / "tokens.txt").read_text(encoding="utf-8").splitlines()}
        if not set(phones) <= tokens:
            raise ValueError("The pronunciation dictionary does not match the model tokens.")

        self.phrase = " ".join(phrase.split())
        self.phones = " ".join(phones)
        with tempfile.NamedTemporaryFile(mode="w+", dir=model_dir.parent, suffix=".txt") as keywords:
            keywords.write(f"{self.phones} @{self.phrase.replace(' ', '_')}\n")
            keywords.flush()
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(model_dir / "tokens.txt"),
                encoder=str(model_dir / MODEL_FILES[0]),
                decoder=str(model_dir / MODEL_FILES[1]),
                joiner=str(model_dir / MODEL_FILES[2]),
                keywords_file=keywords.name,
                num_threads=1,
                keywords_threshold=threshold,
                provider="cpu",
            )
            self._stream = self._spotter.create_stream()

    def reset(self):
        self._stream = self._spotter.create_stream()

    def process(self, pcm: bytes, sample_rate: int) -> list[str]:
        if sample_rate != 16000 or len(pcm) % 2:
            raise ValueError("Expected 16 kHz mono signed 16-bit PCM.")
        if not pcm:
            return []
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._stream.accept_waveform(sample_rate, samples)
        detections = []
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                detections.append(self.phrase)
                self._spotter.reset_stream(self._stream)
        return detections


class WakeWordGate(FrameProcessor):
    def __init__(self, detector, activate):
        super().__init__()
        self._detector = detector
        self._activate = activate
        self._state = "waiting"
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._activation_task = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (CancelFrame, EndFrame)):
            await self._stop()
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            try:
                if frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio) % 2:
                    raise ValueError("Wake word detection requires 16 kHz mono signed 16-bit PCM.")
                async with self._lock:
                    if self._state == "active":
                        await self.push_frame(frame, direction)
                    elif self._state == "connecting":
                        self._buffer.extend(frame.audio)
                        # Bound audio retained during the service's 30-second connection deadline.
                        if len(self._buffer) > 32 * 32000:
                            raise RuntimeError("Gemini connection audio buffer exceeded 32 seconds.")
                    elif self._state == "waiting":
                        self._buffer.extend(frame.audio)
                        del self._buffer[:-32000]
                        detected = await asyncio.to_thread(
                            self._detector.process, frame.audio, frame.sample_rate
                        )
                        if detected:
                            self._state = "connecting"
                            logger.info("Heard Luna. Connecting to Gemini Live…")
                            self._activation_task = self.create_task(self._start_conversation())
            except Exception as error:
                await self._fail(error)
            return
        await self.push_frame(frame, direction)

    async def rearm(self):
        try:
            if self._activation_task and self._activation_task is not asyncio.current_task():
                await self.cancel_task(self._activation_task)
                self._activation_task = None
            async with self._lock:
                if self._state == "stopped":
                    return
                await asyncio.to_thread(self._detector.reset)
                if self._state == "stopped":
                    return
                self._buffer.clear()
                self._state = "waiting"
            logger.info("Waiting for the wake word. Say Luna to start; press Ctrl+C to stop.")
        except Exception as error:
            await self._fail(error)

    async def _start_conversation(self):
        try:
            await self._activate()
            await self.resume()
        except ConnectionError as error:
            logger.warning("{}", error)
            await self.rearm()
        except Exception as error:
            await self._fail(error)

    async def pause(self):
        async with self._lock:
            if self._state == "active":
                self._buffer.clear()
                self._state = "connecting"

    async def resume(self):
        try:
            async with self._lock:
                if self._state != "connecting":
                    return
                if self._buffer:
                    await self.push_frame(InputAudioRawFrame(bytes(self._buffer), 16000, 1))
                self._buffer.clear()
                self._state = "active"
            logger.info("Luna is listening. Speak normally; press Ctrl+C to stop.")
        except Exception as error:
            await self._fail(error)

    async def _fail(self, error):
        self._state = "stopped"
        self._buffer.clear()
        await self.push_error(
            f"Wake word activation failed: {error}", exception=error,
            force_treat_as_permanent=True,
        )

    async def _stop(self):
        self._state = "stopped"
        if self._activation_task is not None:
            await self.cancel_task(self._activation_task)
            self._activation_task = None
        self._buffer.clear()

    async def cleanup(self):
        try:
            await self._stop()
        finally:
            await super().cleanup()
