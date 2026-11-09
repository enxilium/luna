"""Five short amplitude envelopes for the voice indicator, without retaining audio."""

import numpy as np


def pcm_levels(pcm: bytes) -> tuple[float, ...]:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    levels = []
    for window in np.array_split(samples, 5):
        rms = float(np.sqrt(np.mean(window * window))) if window.size else 0.0
        levels.append(float(np.clip((20 * np.log10(max(rms, 1e-6)) + 55) / 40, 0, 1)))
    return tuple(levels)
