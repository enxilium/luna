"""Read-only device inspection, mixer metering, and interpretation for audio_compare."""

import ctypes
import math
import struct
import warnings

import AVFoundation as AVF
import CoreAudio as CA
import numpy as np
import objc


_cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFRelease.restype = None
_au = ctypes.CDLL('/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox')
_au.AudioUnitGetProperty.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
]
_au.AudioUnitGetProperty.restype = ctypes.c_int32


def _property(device: int, selector: int, code: str, *, scope=None, element=0):
    address = CA.AudioObjectPropertyAddress(
        selector, scope or CA.kAudioObjectPropertyScopeGlobal, element,
    )
    if not CA.AudioObjectHasProperty(device, address):
        return None
    size = struct.calcsize(code)
    status, _, data = CA.AudioObjectGetPropertyData(device, address, 0, b'', size, None)
    if status:
        raise RuntimeError(f'CoreAudio property {selector:#x} failed: {status}')
    value = struct.unpack(code, data)[0]
    if code != 'P' or not value:
        return value
    try:
        buffer = ctypes.create_string_buffer(4096)
        if not _cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
            raise RuntimeError('Could not read a CoreAudio device name.')
        return buffer.value.decode('utf-8')
    finally:
        _cf.CFRelease(value)


def device_state(device: int, *, output: bool) -> dict:
    scope = CA.kAudioObjectPropertyScopeOutput if output else CA.kAudioObjectPropertyScopeInput
    transport = _property(device, CA.kAudioDevicePropertyTransportType, 'I')
    return {
        'id': device,
        'name': _property(device, CA.kAudioObjectPropertyName, 'P'),
        'uid': _property(device, CA.kAudioDevicePropertyDeviceUID, 'P'),
        'transport': transport.to_bytes(4, 'big').decode('ascii', errors='replace') if transport else None,
        'sample_rate': _property(device, CA.kAudioDevicePropertyNominalSampleRate, 'd'),
        'volume': [_property(device, CA.kAudioDevicePropertyVolumeScalar, 'f', scope=scope, element=i)
                   for i in (0, 1, 2)],
        'mute': _property(device, CA.kAudioDevicePropertyMute, 'I', scope=scope),
        'data_source': _property(device, CA.kAudioDevicePropertyDataSource, 'I', scope=scope),
    }


def route_ids() -> tuple[int, int]:
    return tuple(_property(CA.kAudioObjectSystemObject, selector, 'I') for selector in (
        CA.kAudioHardwarePropertyDefaultInputDevice, CA.kAudioHardwarePropertyDefaultOutputDevice,
    ))


def route_state() -> dict:
    microphone, output = route_ids()
    return {
        'input': device_state(microphone, output=False),
        'output': device_state(output, output=True),
        'microphone_mode': int(AVF.AVCaptureDevice.activeMicrophoneMode()),
    }


def _format(node, *, output: bool) -> dict:
    fmt = node.outputFormatForBus_(0) if output else node.inputFormatForBus_(0)
    return {'sample_rate': fmt.sampleRate(), 'channels': fmt.channelCount()}


def _unit_device(node) -> int | None:
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', objc.ObjCPointerWarning)
        unit = node.audioUnit()
    device = ctypes.c_uint32()
    size = ctypes.c_uint32(ctypes.sizeof(device))
    status = _au.AudioUnitGetProperty(
        unit.pointerAsInteger, 2000, 0, 0, ctypes.byref(device), ctypes.byref(size),
    )
    return None if status else device.value


def engine_state(audio) -> dict:
    engine = audio._engine
    output = engine.outputNode()
    output_device = _unit_device(output)
    state = {
        'running': bool(engine.isRunning()),
        'player_format': _format(audio._player, output=True),
        'mixer_format': _format(engine.mainMixerNode(), output=True),
        'output_client_format': _format(output, output=False),
        'output_hardware_format': _format(output, output=True),
        'output_unit_device': device_state(output_device, output=True) if output_device else None,
        'output_voice_processing': bool(output.isVoiceProcessingEnabled()),
        'mixer_volume': engine.mainMixerNode().outputVolume(),
        'player_volume': audio._player.volume(),
        'presentation_latency': output.presentationLatency(),
        'microphone_mode': int(AVF.AVCaptureDevice.activeMicrophoneMode()),
    }
    if audio._audio_in_enabled:
        capture = audio._capture_engine
        microphone = capture.inputNode()
        input_device = _unit_device(microphone)
        duck = microphone.voiceProcessingOtherAudioDuckingConfiguration()
        state.update({
            'capture_running': bool(capture.isRunning()),
            'capture_output_voice_processing': bool(capture.outputNode().isVoiceProcessingEnabled()),
            'capture_output_format': _format(capture.outputNode(), output=False),
            'input_client_format': _format(microphone, output=True),
            'input_hardware_format': _format(microphone, output=False),
            'input_unit_device': device_state(input_device, output=False) if input_device else None,
            'input_voice_processing': bool(microphone.isVoiceProcessingEnabled()),
            'bypassed': bool(microphone.isVoiceProcessingBypassed()),
            'agc_enabled': bool(microphone.isVoiceProcessingAGCEnabled()),
            'ducking': {'advanced': bool(duck.enableAdvancedDucking), 'level': int(duck.duckingLevel)},
        })
    return state


async def speech_starts(samples: np.ndarray, rate: int) -> list[float]:
    """Replay a microphone recording through Luna's default VAD at its audio clock."""
    import soxr
    from unittest.mock import patch
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADState

    samples = soxr.resample(samples.astype(np.float32), rate, 16000)
    pcm = np.clip(samples * 32768, -32768, 32767).astype('<i2').tobytes()
    vad = SileroVADAnalyzer(sample_rate=16000)
    vad.set_sample_rate(16000)
    previous = VADState.QUIET
    starts = []
    for offset in range(0, len(pcm) - 1023, 1024):
        seconds = offset / 32000
        with patch('pipecat.audio.vad.silero.time.time', return_value=seconds + 100):
            state = await vad.analyze_audio(pcm[offset:offset + 1024])
        if state == VADState.SPEAKING and previous != VADState.SPEAKING:
            starts.append(round(seconds, 3))
        previous = state
    return starts


def signal_metrics(samples: np.ndarray, rate: int) -> dict:
    if samples.ndim == 1:
        samples = samples[:, None]
    active = np.flatnonzero(np.max(np.abs(samples), axis=1) > 0.0001) if len(samples) else []
    if not len(active):
        return {'active_frames': 0, 'sample_rate': rate}
    samples = samples[active[0]:active[-1] + 1].astype(np.float64)
    mono = samples.mean(axis=1)

    def db(value):
        return round(20 * math.log10(max(float(value), 1e-12)), 3)

    spectrum = np.abs(np.fft.rfft(mono)) ** 2 / len(mono) ** 2
    frequencies = np.fft.rfftfreq(len(mono), 1 / rate)
    return {
        'sample_rate': rate, 'active_frames': len(samples),
        'rms_dbfs': db(np.sqrt(np.mean(mono ** 2))),
        'peak_dbfs': db(np.max(np.abs(samples))),
        'channel_rms_dbfs': [db(np.sqrt(np.mean(channel ** 2))) for channel in samples.T],
        'clipped_samples': int(np.count_nonzero(np.abs(samples) >= 0.9999)),
        'band_db': {f'{low}-{high}': round(10 * math.log10(max(float(spectrum[
            (frequencies >= low) & (frequencies < high)].sum()), 1e-24)), 3)
            for low, high in ((80, 300), (300, 3000), (3000, 10000))},
    }


def interpret(results: list[dict]) -> list[str]:
    findings = []
    by_id = {item['id']: item for item in results if item.get('status') == 'ok'}
    normal = by_id.get('01', {})
    for item in results:
        label = f'Case {item["id"]}'
        if item.get('status') == 'error':
            findings.append(f'{label}: failed; inspect its log and error before interpreting audio.')
        if item.get('case', {}).get('microphone') and item.get('status') == 'ok':
            starts = item.get('microphone_speech_starts', [])
            findings.append(f'{label}: {len(starts)} microphone speech starts; quiet-room playback should produce zero.')
        if item.get('mixer', {}).get('clipped_samples', 0):
            findings.append(f'{label}: the mixer signal clips; investigate upstream gain.')
        before = item.get('route_before')
        if before and any(item.get(key, before) != before for key in
                          ('route_during', 'route_after_playback', 'route_released')):
            findings.append(f'{label}: device rate, volume, route, or microphone mode changed; inspect all snapshots.')
        if item.get('rating') == 'degraded' and normal.get('rating') == 'good':
            findings.append(f'{label}: listening differs from the normal reference despite ordinary playback; inspect route and ducking.')
        if item.get('capture_discontinuities', 0):
            findings.append(f'{label}: microphone sample-time discontinuities were recorded.')
    if normal.get('rating') == by_id.get('02', {}).get('rating') == 'good':
        findings.append('Normal playback and separate native AEC both received good listening ratings.')
    if not findings:
        findings.append('Review configuration checks and listening ratings; silent preparation does not test echo suppression.')
    findings.append('Microphone speech starts can be human speech or echo. Mixer taps do not measure acoustic speaker output or calibrated echo attenuation.')
    return findings


def self_check() -> None:
    rate = 24000
    tone = 0.25 * np.sin(2 * np.pi * 1000 * np.arange(rate) / rate)
    mono = signal_metrics(tone, rate)
    stereo = signal_metrics(np.column_stack((tone, tone)), rate)
    assert mono['rms_dbfs'] == stereo['rms_dbfs']
    assert abs(mono['rms_dbfs'] + 15.05) < 0.1
    assert mono['band_db']['300-3000'] > mono['band_db']['80-300'] + 50
    assert signal_metrics(np.zeros(100), rate)['active_frames'] == 0
    assert signal_metrics(np.ones(100), rate)['clipped_samples'] == 100
    results = [{'id': '01', 'status': 'ok', 'rating': 'good'},
               {'id': '02', 'status': 'ok', 'rating': 'good', 'case': {'microphone': True},
                'microphone_speech_starts': [], 'route_before': {'volume': 1},
                'route_after_playback': {'volume': 0.5}}]
    findings = interpret(results)
    assert any('0 microphone speech starts' in item for item in findings)
    assert any('all snapshots' in item for item in findings)
    assert any('good listening ratings' in item for item in findings)
    print('Signal metering and diagnostic interpretation checks passed.')
