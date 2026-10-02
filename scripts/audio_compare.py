"""Run with: uv run python -m scripts.audio_compare"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import wave

import AVFoundation as AVF
import httpx
import numpy as np
from pipecat.frames.frames import OutputAudioRawFrame

from scripts._audio_diagnostics import (
    engine_state, interpret, route_ids, route_state, self_check, signal_metrics, speech_starts,
)
from src.audio.apple import AppleAudioTransport
from src.clients.voicestudio import BASE_URL


@dataclass(frozen=True)
class Case:
    id: str
    label: str
    microphone: bool = False
    interrupt: bool = False


CASES = (
    Case('01', 'Normal playback reference'),
    Case('02', 'Normal playback + separate native echo cancellation', microphone=True),
    Case('03', 'Separate echo cancellation preserved across interruption', microphone=True, interrupt=True),
    Case('04', 'Normal playback recovery control'),
)
TEXT = "Hello, I'm Luna. My voice should sound clear and warm, with crisp consonants and a full, natural tone."


def _save_json(path: Path, value) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def _save_wav(path: Path, pcm: bytes, rate=24000, channels=1) -> None:
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)


def _validate_state(case: Case, state: dict) -> None:
    if not state['running'] or state['output_voice_processing']:
        raise RuntimeError('The running output mode did not match the requested test.')
    if case.microphone:
        if not all(state[key] for key in ('capture_running', 'input_voice_processing', 'capture_output_voice_processing')):
            raise RuntimeError('Separate native echo cancellation is not running.')
        if state['bypassed'] or state['agc_enabled']:
            raise RuntimeError('Apple did not apply the requested bypass/AGC settings.')
        if state['microphone_mode'] != AVF.AVCaptureMicrophoneModeStandard:
            raise RuntimeError('Use Standard microphone mode in Control Center to match the verified configuration.')
        if state['ducking'] != {'advanced': False, 'level': 10}:
            raise RuntimeError('Apple did not apply minimum other-audio ducking.')
        if state['input_client_format'] != state['capture_output_format']:
            raise RuntimeError('The capture engine I/O formats do not match.')


async def _play(audio: AppleAudioTransport, pcm: bytes, rate: int) -> dict:
    chunk = int(rate / 50) * 2
    waits = []
    started = time.monotonic()
    for offset in range(0, len(pcm), chunk):
        before = time.monotonic()
        frame = OutputAudioRawFrame(pcm[offset:offset + chunk], rate, 1)
        await asyncio.wait_for(audio._write_audio(frame, asyncio.get_running_loop()), len(pcm) / (rate * 2) + 5)
        waits.append(time.monotonic() - before)
    await asyncio.wait_for(audio._drain_playback(), len(pcm) / (rate * 2) + 5)
    # DataRendered precedes device presentation. Allow that latency before shutdown.
    await asyncio.sleep(max(0.8, audio._engine.outputNode().presentationLatency() + 0.2))
    return {'elapsed_seconds': time.monotonic() - started,
            'largest_write_wait_seconds': max(waits), 'writes': len(waits)}


async def _case(case: Case, directory: Path, prepare_only: bool) -> None:
    result = {'id': case.id, 'case': asdict(case), 'status': 'running'}
    path = directory / f'{case.id}.json'
    audio = None
    tapped = False
    captured = []
    microphone_chunks = []
    expected_ids = tuple(json.loads((directory / 'session.json').read_text())['route_ids'])

    async def watch_route():
        while True:
            if route_ids() != expected_ids:
                if audio is not None:
                    audio._clear_playback()
                raise RuntimeError('Default audio devices changed during the test; comparison stopped.')
            await asyncio.sleep(0.25)

    async def guarded(awaitable):
        playback = asyncio.create_task(awaitable)
        watching = asyncio.create_task(watch_route())
        try:
            done, _ = await asyncio.wait((playback, watching), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return await playback
        finally:
            playback.cancel()
            watching.cancel()
            await asyncio.gather(playback, watching, return_exceptions=True)

    try:
        result['route_before'] = route_state()
        if route_ids() != expected_ids:
            raise RuntimeError('Default audio devices changed since this diagnostic run began.')
        pcm = (directory / 'source.pcm').read_bytes()
        rate = 24000
        audio = AppleAudioTransport(audio_in_enabled=case.microphone)

        def record_microphone(buffer, when):
            samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(buffer.frameLength()),
                                    dtype=np.float32).copy()
            microphone_chunks.append((int(when.sampleTime()), samples, int(buffer.format().sampleRate())))

        # Exercise the production graph; only the tap consumer records instead of pushing frames.
        audio._input._capture = record_microphone
        audio._configure(rate)
        mixer = audio._engine.mainMixerNode()
        mixer_rate = int(mixer.outputFormatForBus_(0).sampleRate())

        def capture(buffer, when):
            count = buffer.frameLength()
            channels = buffer.format().channelCount()
            samples = np.column_stack([np.frombuffer(
                buffer.floatChannelData()[i].as_buffer(count), dtype=np.float32,
            ).copy() for i in range(channels)])
            captured.append((int(when.sampleTime()), samples))

        mixer.installTapOnBus_bufferSize_format_block_(0, int(mixer_rate / 10), None, capture)
        tapped = True
        audio._start()
        await asyncio.sleep(0.8)
        result['engine_before'] = engine_state(audio)
        _validate_state(case, result['engine_before'])
        result['route_during'] = route_state()
        result['played_pcm_sha256'] = hashlib.sha256(pcm).hexdigest()
        result['source_rate'] = rate
        result['source_metrics'] = signal_metrics(np.frombuffer(pcm, dtype='<i2') / 32768, rate)
        _save_json(path, result)

        if not prepare_only:
            if case.interrupt:
                await guarded(_play(audio, pcm[:int(rate * 0.4) * 2], rate))
                await audio._write_audio(OutputAudioRawFrame(pcm, rate, 1), asyncio.get_running_loop())
                pending = audio._pending_render
                audio._clear_playback()
                result['interruption_cleared_pending'] = pending.cancelled()
                result['engine_after_interruption'] = engine_state(audio)
                _validate_state(case, result['engine_after_interruption'])
                if not result['interruption_cleared_pending']:
                    raise RuntimeError('Interruption did not cancel queued playback.')
            result['playback'] = await guarded(_play(audio, pcm, rate))
        result['route_after_playback'] = route_state()
        if audio is not None:
            result['engine_after'] = engine_state(audio)
            _validate_state(case, result['engine_after'])
        result['status'] = 'prepared' if prepare_only else 'ok'
    except Exception as error:
        result.update(status='error', error=str(error))
    finally:
        if audio is not None:
            if tapped:
                audio._engine.mainMixerNode().removeTapOnBus_(0)
            try:
                audio._close()
                result['engine_released'] = not audio._engine.isRunning() and (
                    audio._capture_engine is None or (
                        not audio._capture_engine.isRunning() and
                        not audio._capture_engine.inputNode().isVoiceProcessingEnabled()
                    )
                )
                if not result['engine_released']:
                    raise RuntimeError('A native engine or voice processing survived cleanup.')
            except Exception as error:
                result.update(status='error', cleanup_error=str(error))
            if captured:
                samples = np.concatenate([chunk for _, chunk in captured])
                result['mixer'] = signal_metrics(samples, mixer_rate)
                result['mixer_discontinuities'] = sum(
                    current[0] != previous[0] + len(previous[1])
                    for previous, current in zip(captured, captured[1:])
                )
                _save_wav(directory / f'{case.id}-mixer.wav',
                          np.clip(np.rint(samples * 32768), -32768, 32767).astype('<i2').tobytes(),
                          mixer_rate, samples.shape[1])
            if microphone_chunks:
                samples = np.concatenate([chunk for _, chunk, _ in microphone_chunks])
                native_rate = microphone_chunks[0][2]
                result['capture_frames'] = len(samples)
                result['capture_discontinuities'] = sum(
                    current[0] != previous[0] + len(previous[1])
                    for previous, current in zip(microphone_chunks, microphone_chunks[1:])
                )
                result['microphone_speech_starts'] = await speech_starts(samples, native_rate)
                _save_wav(directory / f'{case.id}-microphone.wav',
                          np.clip(samples * 32768, -32768, 32767).astype('<i2').tobytes(), native_rate)
            elif case.microphone:
                result.update(status='error', error='No microphone buffers were captured.')
        result['route_released'] = route_state()
        _save_json(path, result)


def _report(directory: Path, session: dict, results: list[dict]) -> None:
    findings = interpret(results)
    _save_json(directory / 'report.json', {'session': session, 'results': results, 'findings': findings})
    lines = ['# Luna audio diagnostic', '', f"Output: {session['route']['output']['name']}",
             f"Input: {session['route']['input']['name']}", '',
             '| Case | Test | Result | Listening |', '| --- | --- | --- | --- |']
    lines.extend(f"| {item['id']} | {item['case']['label']} | {item['status']} | {item.get('rating', '')} |" for item in results)
    lines.extend(['', '## Findings', ''] + [f'- {finding}' for finding in findings])
    lines.extend(['', '## Listening notes', ''] + [f"- {item['id']}: {item['note']}" for item in results if item.get('note')])
    (directory / 'report.md').write_text('\n'.join(lines) + '\n')


def run(args) -> None:
    directory = Path(args.report_dir) if args.report_dir else Path('diagnostics/audio') / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    session = {'platform': platform.platform(), 'python': sys.version, 'route': route_state(),
               'route_ids': route_ids(), 'text': TEXT, 'prepare_only': args.prepare_only}
    print(f"Output: {session['route']['output']['name']}\nInput: {session['route']['input']['name']}\nReport: {directory}", flush=True)
    print('Keep devices and volume fixed. Close Luna and other microphone apps. Use Standard microphone mode.', flush=True)
    if args.prepare_only:
        pcm = bytes(4800)  # No synthesis or playback in preparation checks.
    elif args.source_pcm:
        pcm = Path(args.source_pcm).read_bytes()
    else:
        print('Synthesizing one MLX-Audio sample for all tests...', flush=True)
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f'{BASE_URL}/audio/speech', json={
                'model': 'mlx-audio', 'voice': 'alloy', 'input': TEXT, 'response_format': 'pcm',
            })
            response.raise_for_status()
            pcm = response.content
    if not pcm or len(pcm) % 2 or pcm.startswith(b'RIFF'):
        raise RuntimeError('VoiceStudio returned invalid 16-bit PCM audio.')
    session['source_sha256'] = hashlib.sha256(pcm).hexdigest()
    (directory / 'source.pcm').write_bytes(pcm)
    _save_wav(directory / 'source.wav', pcm)
    _save_json(directory / 'session.json', session)
    results = []
    _report(directory, session, results)
    for case in CASES:
        if route_ids() != tuple(session['route_ids']):
            print('Default devices changed; stopping to avoid an invalid comparison.', flush=True)
            break
        print(f'\n{case.id}/{len(CASES):02d}: {case.label}', flush=True)
        # A native format assertion must not kill the entire diagnostic session.
        command = [sys.executable, '-m', 'scripts.audio_compare', '--case', case.id,
                   '--report-dir', str(directory)]
        if args.prepare_only:
            command.append('--prepare-only')
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        (directory / f'{case.id}.log').write_bytes(output)
        path = directory / f'{case.id}.json'
        result = json.loads(path.read_text()) if path.exists() else {'id': case.id, 'case': asdict(case)}
        if timed_out or process.returncode or result.get('status') == 'running':
            result.update(status='error', error=f'Native test exited with code {process.returncode}; timeout={timed_out}; see {case.id}.log.')
        print(f"  {result['status']}: {result.get('error', '')}", flush=True)
        results.append(result)
        _report(directory, session, results)
        if not args.prepare_only and not args.no_prompts and result['status'] == 'ok':
            answer = input('Sound: [g]ood / [d]egraded / [u]nsure, optionally followed by a note: ').strip().split(maxsplit=1)
            result['rating'] = {'g': 'good', 'good': 'good', 'd': 'degraded', 'degraded': 'degraded'}.get(answer[0].lower() if answer else '', 'unsure')
            result['note'] = answer[1] if len(answer) > 1 else ''
            _report(directory, session, results)
        time.sleep(0.8)
    print('\n' + '\n'.join(interpret(results)), flush=True)
    print(f'\nSaved report: {directory / "report.md"}', flush=True)
    if len(results) != len(CASES) or any(item['status'] == 'error' for item in results):
        raise RuntimeError('One or more audio checks failed; see the saved report.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Compare normal playback with Luna\'s separate native echo cancellation.')
    parser.add_argument('--no-prompts', action='store_true', help='Play every case without asking for listening ratings.')
    parser.add_argument('--prepare-only', action='store_true', help='Verify configurations silently, without synthesis or playback.')
    parser.add_argument('--self-check', action='store_true', help='Check metering/interpretation without opening audio devices.')
    parser.add_argument('--report-dir', help='New directory in which to save the diagnostic report.')
    parser.add_argument('--source-pcm', help='Reuse an existing 24 kHz mono int16 clip instead of synthesizing.')
    parser.add_argument('--case', choices=[case.id for case in CASES], help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
            assert len({case.id for case in CASES}) == len(CASES)
            for case in CASES:
                state = {
                    'running': True, 'output_voice_processing': False,
                    'capture_running': True, 'input_voice_processing': True,
                    'capture_output_voice_processing': True, 'bypassed': False,
                    'agc_enabled': False, 'microphone_mode': 0,
                    'ducking': {'advanced': False, 'level': 10},
                    'input_client_format': {'sample_rate': 48000, 'channels': 1},
                    'capture_output_format': {'sample_rate': 48000, 'channels': 1},
                }
                _validate_state(case, state)
                if case.microphone:
                    state['bypassed'] = True
                    try:
                        _validate_state(case, state)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('An unapplied bypass setting must fail the test.')
            print('Case matrix and runtime setting checks passed.')
        elif args.case:
            if not args.report_dir:
                parser.error('--case requires --report-dir')
            asyncio.run(_case(next(case for case in CASES if case.id == args.case), Path(args.report_dir), args.prepare_only))
        else:
            run(args)
    except KeyboardInterrupt:
        parser.exit(130, '\nStopped. Completed results were saved.\n')
    except Exception as error:
        parser.exit(1, f'\nDiagnostic stopped: {error}\n')


if __name__ == '__main__':
    main()
