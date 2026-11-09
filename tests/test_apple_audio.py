"""Native graph ownership and Pipecat playback regression checks; no device access."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import AVFoundation as AVF
from pipecat.frames.frames import (
    CancelFrame, EndFrame, InterruptionFrame, OutputAudioRawFrame, PipelineFlushFrame, StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport

from src.audio.apple import AppleAudioTransport


def engine():
    native = MagicMock()
    microphone = native.inputNode.return_value
    microphone.inputFormatForBus_.return_value.sampleRate.return_value = 48000
    state = {'running': False, 'processing': False}

    def start(_):
        state['running'] = True
        return True, None

    def processing(enabled, _):
        state['processing'] = enabled
        return True, None

    native.startAndReturnError_.side_effect = start
    native.stop.side_effect = lambda: state.update(running=False)
    native.isRunning.side_effect = lambda: state['running']
    microphone.setVoiceProcessingEnabled_error_.side_effect = processing
    microphone.isVoiceProcessingEnabled.side_effect = lambda: state['processing']
    native.attachedNodes.return_value.containsObject_.return_value = True
    return native


class AppleAudioTests(unittest.IsolatedAsyncioTestCase):
    def make_audio(self, capture=True):
        playback, microphone = engine(), engine()
        engines = patch.object(AVF, 'AVAudioEngine')
        players = patch.object(AVF, 'AVAudioPlayerNode')
        factory = engines.start()
        player_factory = players.start()
        self.addCleanup(engines.stop)
        self.addCleanup(players.stop)
        factory.alloc.return_value.init.side_effect = [playback, microphone]
        player_factory.alloc.return_value.init.side_effect = [MagicMock(), MagicMock()]
        audio = AppleAudioTransport(audio_in_enabled=capture)
        self.addCleanup(audio._close)
        return audio, playback, microphone

    async def test_processing_is_confined_to_silent_capture_engine(self):
        audio, playback, capture = self.make_audio()
        audio._configure(24000)
        audio._start()
        playback.inputNode.assert_not_called()
        playback.outputNode.return_value.setVoiceProcessingEnabled_error_.assert_not_called()
        microphone = capture.inputNode.return_value
        microphone.setVoiceProcessingEnabled_error_.assert_called_once_with(True, None)
        microphone.setVoiceProcessingBypassed_.assert_called_once_with(False)
        microphone.setVoiceProcessingAGCEnabled_.assert_called_once_with(False)
        duck = microphone.setVoiceProcessingOtherAudioDuckingConfiguration_.call_args.args[0]
        self.assertFalse(duck.enableAdvancedDucking)
        self.assertEqual(duck.duckingLevel, AVF.AVAudioVoiceProcessingOtherAudioDuckingLevelMin)
        playback.connect_to_format_.assert_any_call(
            playback.mainMixerNode(), playback.outputNode(), None)
        capture_format = microphone.installTapOnBus_bufferSize_format_block_.call_args.args[2]
        self.assertEqual(capture_format.channelCount(), 1)
        self.assertEqual(capture_format.sampleRate(), 48000)
        capture.connect_to_format_.assert_any_call(
            capture.mainMixerNode(), capture.outputNode(), capture_format)
        audio._silent_player.play.assert_called_once()
        audio._silent_player.scheduleBuffer_completionCallbackType_completionHandler_.assert_not_called()

    async def test_playback_only_never_opens_microphone(self):
        audio, playback, capture = self.make_audio(capture=False)
        audio._configure(24000)
        audio._start()
        audio._close()
        self.assertIsNone(audio._capture_engine)
        playback.inputNode.assert_not_called()
        capture.inputNode.assert_not_called()
        capture.startAndReturnError_.assert_not_called()

    async def test_delivery_and_render_events_do_not_report_cancelled_audio(self):
        audio, playback, capture = self.make_audio(capture=False)
        output = audio.output()
        rendered = asyncio.get_running_loop().create_future()
        audio._pending_render = rendered
        output.create_task = asyncio.create_task
        output.get_event_loop = asyncio.get_running_loop
        delivered, completed = [], []

        @output.event_handler('on_audio_delivered')
        async def on_delivery(transport, frame):
            delivered.append(frame)

        @output.event_handler('on_audio_rendered')
        async def on_render(transport, frame):
            completed.append(frame)

        frame = OutputAudioRawFrame(b'\0\0', 24000, 1)
        with patch.object(audio, '_write_audio', AsyncMock(return_value=True)):
            self.assertTrue(await output.write_audio_frame(frame))
        self.assertEqual(delivered, [frame])
        self.assertEqual(completed, [])
        rendered.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(completed, [frame])
        cancelled = asyncio.get_running_loop().create_future()
        audio._pending_render = cancelled
        with patch.object(audio, '_write_audio', AsyncMock(return_value=True)):
            await output.write_audio_frame(frame)
        cancelled.cancel()
        await asyncio.sleep(0)
        self.assertEqual(completed, [frame])

    async def test_configuration_and_start_failures_release_both_engines(self):
        for failure in ('playback_prepare', 'enable', 'format', 'capture_prepare',
                        'playback_start', 'capture_start'):
            with self.subTest(failure=failure):
                audio, playback, capture = self.make_audio()
                if failure.endswith('prepare'):
                    failing = playback if failure == 'playback_prepare' else capture
                    failing.prepare.side_effect = RuntimeError('unavailable')
                if failure == 'enable':
                    capture.inputNode().setVoiceProcessingEnabled_error_.return_value = False, 'unavailable'
                    capture.inputNode().setVoiceProcessingEnabled_error_.side_effect = None
                if failure == 'format':
                    capture.inputNode().inputFormatForBus_().sampleRate.return_value = 0
                if failure.endswith('start'):
                    failing = playback if failure == 'playback_start' else capture
                    failing.startAndReturnError_.side_effect = None
                    failing.startAndReturnError_.return_value = False, 'unavailable'
                with self.assertRaises(RuntimeError):
                    audio._configure(24000)
                    audio._start()
                self.assertFalse(playback.isRunning())
                self.assertFalse(capture.isRunning())
                self.assertFalse(audio._tap_installed)
                self.assertTrue(audio._closed)
                playback.detachNode_.assert_called_once_with(audio._player)
                capture.stop.assert_called_once()
                if failure == 'playback_prepare':
                    capture.inputNode.assert_not_called()
                with self.assertRaises(RuntimeError):
                    audio._start()

    async def test_interruption_cancels_backlog_without_resetting_capture(self):
        audio, playback, capture = self.make_audio()
        audio._configure(24000)
        audio._start()
        audio._input._capturing = True
        pending = asyncio.get_running_loop().create_future()
        audio._pending_render = pending
        with patch.object(BaseOutputTransport, 'process_frame', AsyncMock()):
            await audio.output().process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertTrue(pending.cancelled())
        self.assertIsNone(audio._pending_render)
        self.assertTrue(audio._input._capturing)
        self.assertTrue(capture.isRunning())
        capture.stop.assert_not_called()
        audio._silent_player.stop.assert_not_called()
        audio._player.stop.assert_called_once()
        self.assertEqual(playback.startAndReturnError_.call_count, 1)

    async def test_shutdown_is_idempotent_for_stop_cancel_and_cleanup(self):
        for method, frame in (('stop', EndFrame()), ('cancel', CancelFrame()),
                              ('cleanup', None), ('input_cleanup', None)):
            with self.subTest(method=method):
                audio, playback, capture = self.make_audio()
                audio._configure(24000)
                audio._start()
                if method == 'input_cleanup':
                    await audio.input().cleanup()
                elif frame is None:
                    await getattr(audio.output(), method)()
                else:
                    await getattr(audio.output(), method)(frame)
                audio._close()
                self.assertFalse(playback.isRunning())
                self.assertFalse(capture.isRunning())
                playback.stop.assert_called_once()
                capture.stop.assert_called_once()
                capture.inputNode().removeTapOnBus_.assert_called_once_with(0)
                self.assertFalse(capture.inputNode().isVoiceProcessingEnabled())

    async def test_one_failed_release_does_not_skip_other_engine(self):
        audio, playback, capture = self.make_audio()
        audio._configure(24000)
        audio._start()
        audio._player.stop.side_effect = RuntimeError('player failure')
        with self.assertRaises(ExceptionGroup):
            audio._close()
        self.assertFalse(playback.isRunning())
        self.assertFalse(capture.isRunning())
        self.assertFalse(audio._tap_installed)
        audio._player.stop.side_effect = None
        audio._close()

    async def test_render_backpressure_and_capture_health(self):
        audio, playback, capture = self.make_audio()
        audio._configure(24000)
        audio._start()
        loop = asyncio.get_running_loop()
        frame = OutputAudioRawFrame(b'\0' * 960, 24000, 1)
        await audio._write_audio(frame, loop)
        first = audio._pending_render
        second_write = asyncio.create_task(audio._write_audio(frame, loop))
        await asyncio.sleep(0)
        self.assertFalse(second_write.done())
        first.set_result(None)
        self.assertTrue(await second_write)
        flushed = asyncio.create_task(audio.output().write_transport_frame(PipelineFlushFrame()))
        await asyncio.sleep(0)
        self.assertFalse(flushed.done())
        callback = audio._player.scheduleBuffer_completionCallbackType_completionHandler_.call_args.args[2]
        callback(AVF.AVAudioPlayerNodeCompletionDataRendered)
        await audio._drain_playback()
        await flushed
        capture.stop()
        with self.assertRaisesRegex(RuntimeError, 'echo canceller stopped'):
            await audio._write_audio(frame, loop)

    async def test_stalled_render_reports_permanent_pipecat_failure(self):
        audio, _, _ = self.make_audio()
        output = audio.output()
        output.push_error = AsyncMock()
        output._params.audio_out_write_timeout_secs = 0.01
        audio._pending_render = asyncio.get_running_loop().create_future()
        await output._wait_for_playback()
        output.push_error.assert_awaited_once()
        self.assertTrue(output.push_error.call_args.kwargs['force_treat_as_permanent'])
        self.assertTrue(audio._pending_render.cancelled())

    async def test_failed_startup_never_declares_transport_ready(self):
        for capture in (False, True):
            with self.subTest(capture=capture):
                audio, _, _ = self.make_audio(capture=capture)
                audio._start = MagicMock(side_effect=RuntimeError('no IO cycle'))
                transport = audio.input() if capture else audio.output()
                transport.push_error = AsyncMock()
                transport.set_transport_ready = AsyncMock()
                base = BaseInputTransport if capture else BaseOutputTransport
                with patch.object(base, 'start', AsyncMock()):
                    await transport.start(StartFrame())
                transport.set_transport_ready.assert_not_awaited()
                self.assertTrue(transport.push_error.call_args.kwargs['force_treat_as_permanent'])
                if capture:
                    self.assertFalse(transport._capturing)

    async def test_capture_watchdog_detects_stopped_engines_and_missing_callbacks(self):
        for failure in ('callbacks', 'capture', 'playback'):
            with self.subTest(failure=failure):
                audio, playback, capture = self.make_audio()
                audio._configure(24000)
                audio._start()
                source = audio.input()
                source._capturing = True
                source._last_capture = 0 if failure == 'callbacks' else 99
                source.push_error = AsyncMock()
                if failure != 'callbacks':
                    (capture if failure == 'capture' else playback).stop()
                with (
                    patch('src.audio.apple.time.monotonic', return_value=100),
                    patch('src.audio.apple.asyncio.sleep', new_callable=AsyncMock),
                ):
                    await source._monitor_capture()
                source.push_error.assert_awaited_once()
                self.assertTrue(source.push_error.call_args.kwargs['force_treat_as_permanent'])
                self.assertFalse(source._capturing)

    async def test_capture_watchdog_is_cancelled_with_capture(self):
        audio, _, _ = self.make_audio()
        source = audio.input()
        source.create_task = asyncio.create_task

        async def cancel(task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        source.cancel_task = cancel
        with (
            patch.object(BaseInputTransport, 'start', new_callable=AsyncMock),
            patch.object(BaseInputTransport, 'cancel', new_callable=AsyncMock),
            patch.object(source, 'set_transport_ready', new_callable=AsyncMock),
        ):
            await source.start(StartFrame())
            watchdog = source._health_task
            await source.cancel(CancelFrame())
        self.assertTrue(watchdog.cancelled())
        self.assertIsNone(source._health_task)

    async def test_sleep_gap_rebuilds_even_if_engines_have_resumed(self):
        audio, _, _ = self.make_audio()
        audio._configure(24000)
        audio._start()
        source = audio.input()
        source._capturing = True
        source._last_capture = 99
        source.push_error = AsyncMock()
        with (
            patch('src.audio.apple.time.time', side_effect=[100, 200]),
            patch('src.audio.apple.time.monotonic', return_value=100),
            patch('src.audio.apple.asyncio.sleep', new_callable=AsyncMock),
        ):
            await source._monitor_capture()
        source.push_error.assert_awaited_once()
        self.assertFalse(source._capturing)



if __name__ == '__main__':
    unittest.main()
