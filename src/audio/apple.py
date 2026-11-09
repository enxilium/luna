"""Local duplex audio with Apple's echo cancellation before Pipecat speech detection."""

import asyncio
import time

import AVFoundation as AVF
import numpy as np
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams


class AppleAudioTransport(BaseTransport):
    """Normal speaker playback with a separate, silent native echo canceller."""

    def __init__(self, *, audio_in_enabled: bool = True):
        super().__init__()
        params = TransportParams(audio_in_enabled=audio_in_enabled, audio_out_enabled=True)
        self._audio_in_enabled = audio_in_enabled
        self._engine = AVF.AVAudioEngine.alloc().init()
        self._player = AVF.AVAudioPlayerNode.alloc().init()
        self._capture_engine = AVF.AVAudioEngine.alloc().init() if audio_in_enabled else None
        self._silent_player = AVF.AVAudioPlayerNode.alloc().init() if audio_in_enabled else None
        self._microphone = None
        self._playback_format = None
        self._pending_render: asyncio.Future[None] | None = None
        self._tap_installed = False
        self._closed = False
        self._input = _AppleInputTransport(self, params)
        self._output = _AppleOutputTransport(self, params)

    def input(self) -> BaseInputTransport:
        return self._input

    def output(self) -> BaseOutputTransport:
        return self._output

    def _configure(self, output_sample_rate: int) -> None:
        if self._closed:
            raise RuntimeError("Apple audio transport is closed.")
        try:
            self._playback_format = (
                AVF.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
                    output_sample_rate, 1
                )
            )
            self._engine.attachNode_(self._player)
            mixer = self._engine.mainMixerNode()
            self._engine.connect_to_format_(self._player, mixer, self._playback_format)
            self._engine.connect_to_format_(mixer, self._engine.outputNode(), None)
            self._engine.prepare()

            if self._capture_engine is not None:
                microphone = self._microphone = self._capture_engine.inputNode()
                enabled, error = microphone.setVoiceProcessingEnabled_error_(True, None)
                if not enabled:
                    raise RuntimeError(f"Could not enable Apple voice processing: {error}")
                microphone.setVoiceProcessingBypassed_(False)
                microphone.setVoiceProcessingAGCEnabled_(False)
                microphone.setVoiceProcessingOtherAudioDuckingConfiguration_(
                    AVF.AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                        False, AVF.AVAudioVoiceProcessingOtherAudioDuckingLevelMin
                    )
                )
                native_rate = microphone.inputFormatForBus_(0).sampleRate()
                if native_rate <= 0:
                    raise RuntimeError("No microphone is available. Check macOS Sound settings.")
                io_format = AVF.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
                    native_rate, 1
                )
                # VPIO needs matching I/O inside this engine. Its output stays silent;
                # macOS supplies system playback as the microphone's echo reference.
                self._capture_engine.attachNode_(self._silent_player)
                mixer = self._capture_engine.mainMixerNode()
                self._capture_engine.connect_to_format_(self._silent_player, mixer, io_format)
                self._capture_engine.connect_to_format_(
                    mixer, self._capture_engine.outputNode(), io_format
                )
                microphone.installTapOnBus_bufferSize_format_block_(
                    0, int(native_rate / 50), io_format, self._input._capture
                )
                self._tap_installed = True
                self._capture_engine.prepare()
        except Exception:
            self._close()
            raise

    def _start(self) -> None:
        if self._closed:
            raise RuntimeError("Apple audio transport is closed.")
        try:
            started, error = self._engine.startAndReturnError_(None)
            if not started:
                raise RuntimeError(f"Could not start Apple speaker playback: {error}")
            self._player.play()
            if self._capture_engine is not None:
                started, error = self._capture_engine.startAndReturnError_(None)
                if not started:
                    raise RuntimeError(f"Could not start Apple echo cancellation: {error}")
                self._silent_player.play()
                logger.info("Normal speaker playback and separate Apple echo cancellation are enabled.")
            else:
                logger.info("Apple audio output is enabled; microphone capture is disabled.")
        except Exception:
            self._close()
            raise

    def _clear_playback(self) -> None:
        # Keep the engine running so interruption does not reset the echo canceller.
        if self._pending_render is not None:
            self._pending_render.cancel()
            self._pending_render = None
        self._player.stop()
        if self._engine.isRunning():
            self._player.play()

    def _close(self) -> None:
        if self._closed:
            return
        self._input._capturing = False
        if self._pending_render is not None:
            self._pending_render.cancel()
            self._pending_render = None
        errors = []
        pairs = [(self._engine, self._player)]
        if self._capture_engine is not None:
            pairs.append((self._capture_engine, self._silent_player))
        # Attempt every release even if one native call fails.
        for engine, player in pairs:
            for release in (player.stop, engine.stop):
                try:
                    release()
                except Exception as error:
                    errors.append(error)
        if self._tap_installed:
            try:
                self._microphone.removeTapOnBus_(0)
                self._tap_installed = False
            except Exception as error:
                errors.append(error)
        if self._microphone is not None:
            try:
                microphone = self._microphone
                if microphone.isVoiceProcessingEnabled():
                    released, error = microphone.setVoiceProcessingEnabled_error_(False, None)
                    if not released:
                        raise RuntimeError(f"Could not release Apple voice processing: {error}")
            except Exception as error:
                errors.append(error)
        for engine, player in pairs:
            try:
                if engine.attachedNodes().containsObject_(player):
                    engine.detachNode_(player)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Could not release Apple audio", errors)
        self._closed = True

    async def _write_audio(
        self, frame: OutputAudioRawFrame, loop: asyncio.AbstractEventLoop
    ) -> bool:
        if not self._engine.isRunning():
            raise RuntimeError("The audio engine stopped. Check audio devices and restart Luna.")
        if self._capture_engine is not None and not self._capture_engine.isRunning():
            raise RuntimeError("The echo canceller stopped. Restart Luna before continuing playback.")
        if frame.num_channels != 1 or frame.sample_rate != self._playback_format.sampleRate():
            raise ValueError("Apple playback requires mono PCM at the transport sample rate.")
        samples = np.frombuffer(frame.audio, dtype="<i2")
        if not len(samples):
            return True
        buffer = AVF.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            self._playback_format, len(samples)
        )
        buffer.setFrameLength_(len(samples))
        channel = np.frombuffer(
            buffer.floatChannelData()[0].as_buffer(len(samples)), dtype=np.float32
        )
        channel[:] = samples / 32768.0
        rendered = loop.create_future()

        def finish():
            if not rendered.done():
                rendered.set_result(None)

        def on_rendered(callback_type):
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish)

        self._player.scheduleBuffer_completionCallbackType_completionHandler_(
            buffer, AVF.AVAudioPlayerNodeCompletionDataRendered, on_rendered
        )
        # Schedule one chunk ahead to avoid gaps at render boundaries.
        previous = self._pending_render
        self._pending_render = rendered
        if previous is not None:
            await previous
        return not rendered.cancelled()

    async def _drain_playback(self) -> None:
        if self._pending_render is not None:
            await self._pending_render


class _AppleInputTransport(BaseInputTransport):
    def __init__(self, audio: AppleAudioTransport, params: TransportParams):
        super().__init__(params)
        self._audio = audio
        self._register_event_handler("on_audio_captured", sync=True)
        self._capturing = False
        self._resampler = create_stream_resampler()
        self._last_capture = 0.0
        self._health_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._capturing = False
        try:
            self._audio._start()
        except Exception as error:
            self._capturing = False
            await self.push_error(f"Apple audio startup failed: {error}", exception=error,
                                  force_treat_as_permanent=True)
            return
        await self.set_transport_ready(frame)
        self._capturing = True
        self._last_capture = time.monotonic()
        self._health_task = self.create_task(self._monitor_capture())

    async def _monitor_capture(self):
        last_check = time.time()
        while self._capturing:
            await asyncio.sleep(1)
            if not self._capturing:
                return
            now = time.time()
            if (
                now - last_check > 5
                or time.monotonic() - self._last_capture > 5
                or not self._audio._engine.isRunning()
                or not self._audio._capture_engine.isRunning()
            ):
                self._capturing = False
                await self.push_error(
                    "Apple audio stopped delivering microphone audio; rebuilding the audio pipeline.",
                    force_treat_as_permanent=True,
                )
                return
            last_check = now

    async def _stop_capture(self):
        self._capturing = False
        if self._health_task is not None:
            await self.cancel_task(self._health_task)
            self._health_task = None

    async def stop(self, frame: EndFrame):
        await self._stop_capture()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await self._stop_capture()
        await super().cancel(frame)

    async def cleanup(self):
        await self._stop_capture()
        try:
            await super().cleanup()
        finally:
            self._audio._close()

    def _capture(self, buffer, when):
        if not self._capturing:
            return
        # Copy inside the native callback: Apple's buffer is reused after it returns.
        samples = np.frombuffer(
            buffer.floatChannelData()[0].as_buffer(buffer.frameLength()), dtype=np.float32
        )
        audio = np.clip(samples * 32768.0, -32768, 32767).astype("<i2").tobytes()
        self.get_event_loop().call_soon_threadsafe(
            self._receive_audio, audio, int(buffer.format().sampleRate())
        )

    def _receive_audio(self, audio: bytes, native_rate: int):
        if self._capturing:
            self._last_capture = time.monotonic()
            self.create_task(self._push_audio(audio, native_rate))

    async def _push_audio(self, audio: bytes, native_rate: int):
        try:
            audio = await self._resampler.resample(audio, native_rate, self.sample_rate)
            if self._capturing and audio:
                frame = InputAudioRawFrame(audio, self.sample_rate, 1)
                await self._call_event_handler("on_audio_captured", frame)
                await self.push_audio_frame(frame)
        except Exception as error:
            self._capturing = False
            await self.push_error(
                f"Apple microphone processing failed: {error}",
                exception=error,
                force_treat_as_permanent=True,
            )


class _AppleOutputTransport(BaseOutputTransport):
    def __init__(self, audio: AppleAudioTransport, params: TransportParams):
        super().__init__(params)
        self._audio = audio
        self._register_event_handler("on_audio_delivered", sync=True)
        self._register_event_handler("on_audio_rendered", sync=True)
        self._register_event_handler("on_playback_stopped", sync=True)

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._audio._configure(self.sample_rate)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if not self._audio._audio_in_enabled:
            try:
                self._audio._start()
            except Exception as error:
                await self.push_error(f"Apple audio startup failed: {error}", exception=error,
                                      force_treat_as_permanent=True)
                return
        await self.set_transport_ready(frame)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, InterruptionFrame):
            self._audio._clear_playback()
            await self._call_event_handler("on_playback_stopped")
        await super().process_frame(frame, direction)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        try:
            delivered = await self._audio._write_audio(frame, self.get_event_loop())
            if delivered and frame.audio:
                await self._call_event_handler("on_audio_delivered", frame)
                rendered = self._audio._pending_render
                if rendered is not None and self._event_handlers["on_audio_rendered"].handlers:
                    def on_rendered(future):
                        if not future.cancelled():
                            self.create_task(self._call_event_handler("on_audio_rendered", frame))
                    rendered.add_done_callback(on_rendered)
            return delivered
        except asyncio.CancelledError:
            self._audio._clear_playback()
            raise
        except Exception as error:
            await self.push_error(
                f"Apple audio playback failed: {error}",
                exception=error,
                force_treat_as_permanent=True,
            )
            return False

    async def write_transport_frame(self, frame: Frame):
        # Deliver synchronized text and control frames after preceding audio renders.
        await self._wait_for_playback()

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        if isinstance(frame, BotStoppedSpeakingFrame):
            await self._wait_for_playback()
            if direction == FrameDirection.DOWNSTREAM:
                await self._call_event_handler("on_playback_stopped")
        await super().push_frame(frame, direction)

    async def _wait_for_playback(self):
        try:
            await asyncio.wait_for(
                self._audio._drain_playback(),
                timeout=self._params.audio_out_write_timeout_secs,
            )
        except TimeoutError as error:
            await self.push_error(
                "Apple audio playback stalled. Check audio devices and restart Luna.",
                exception=error,
                force_treat_as_permanent=True,
            )

    async def stop(self, frame: EndFrame):
        try:
            await super().stop(frame)
            await self._wait_for_playback()
        finally:
            self._audio._close()

    async def cancel(self, frame: CancelFrame):
        try:
            await super().cancel(frame)
        finally:
            self._audio._close()

    async def cleanup(self):
        try:
            await super().cleanup()
        finally:
            self._audio._close()
