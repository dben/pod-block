"""Landmark audio fingerprints.

Audio is decoded to 8 kHz mono, turned into a log-magnitude spectrogram and
reduced to a sparse set of spectral peaks. Each peak (the "anchor") is paired
with a few peaks that follow it closely in time, and each pair is packed into
a 32-bit hash of (anchor freq, freq delta, time delta). Two recordings of the
same audio share many hashes at a constant time offset, even after lossy
re-encoding, volume changes or added noise. That constant offset is what the
matcher looks for.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import maximum_filter

SAMPLE_RATE = 8000
N_FFT = 512
HOP = 256
FRAME_SEC = HOP / SAMPLE_RATE  # 32 ms per frame

# Peak picking: a bin is a peak when it is the largest value within this many
# frames / bins of itself and loud enough to stand out from the block.
PEAK_TIME_SIZE = 21
PEAK_FREQ_SIZE = 21
PEAK_MIN_DB_ABOVE_MEDIAN = 10.0
PEAK_ABS_FLOOR_DB = -50.0
MIN_FREQ_BIN = 5  # ignore everything below ~80 Hz (hum, rumble)

# Pairing: each anchor is paired with up to FAN_OUT later peaks inside this zone.
FAN_OUT = 5
TARGET_MAX_DT = 63  # frames, fits in 6 bits (~2 s)
TARGET_MAX_DF = 127  # bins, fits in 8 bits as a signed value
PAIR_LOOKAHEAD = 40

BLOCK_FRAMES = 8192  # spectrogram is built in blocks of ~4.4 minutes


@dataclass
class Fingerprint:
    hashes: np.ndarray  # uint32
    times: np.ndarray  # int32, anchor frame index
    duration: float  # seconds

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path, hashes=self.hashes, times=self.times, duration=np.float64(self.duration)
        )

    @classmethod
    def load(cls, path: Path) -> "Fingerprint":
        with np.load(path) as data:
            return cls(data["hashes"], data["times"], float(data["duration"]))

    def slice(self, start_sec: float, end_sec: float) -> "Fingerprint":
        """Hashes whose anchor falls inside [start, end), re-timed to start at 0."""
        lo, hi = seconds_to_frames(start_sec), seconds_to_frames(end_sec)
        keep = (self.times >= lo) & (self.times < hi)
        return Fingerprint(self.hashes[keep], self.times[keep] - lo, end_sec - start_sec)


def seconds_to_frames(sec: float) -> int:
    return int(round(sec / FRAME_SEC))


def frames_to_seconds(frames) -> float:
    return float(frames) * FRAME_SEC


def decode_audio(path: str | Path) -> np.ndarray:
    """Decode any format ffmpeg understands into 8 kHz mono int16 samples."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {path}: {proc.stderr.decode(errors='replace')}")
    return np.frombuffer(proc.stdout, dtype=np.int16)


def spectral_peaks(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (frame index, freq bin) of the spectral peaks, sorted by time."""
    if samples.dtype != np.float32:
        samples = samples.astype(np.float32) / 32768.0
    n_frames = 1 + (len(samples) - N_FFT) // HOP if len(samples) >= N_FFT else 0
    if n_frames <= 0:
        return np.zeros(0, np.int32), np.zeros(0, np.int32)

    window = np.hanning(N_FFT).astype(np.float32)
    all_frames = np.lib.stride_tricks.sliding_window_view(samples, N_FFT)[::HOP]
    margin = PEAK_TIME_SIZE // 2 + 1
    times, freqs = [], []

    for b0 in range(0, n_frames, BLOCK_FRAMES):
        b1 = min(b0 + BLOCK_FRAMES, n_frames)
        lo, hi = max(0, b0 - margin), min(n_frames, b1 + margin)
        spec = np.abs(np.fft.rfft(all_frames[lo:hi] * window, axis=1)).astype(np.float32)
        db = 20.0 * np.log10(spec + 1e-6)
        db[:, :MIN_FREQ_BIN] = -200.0

        local_max = maximum_filter(db, size=(PEAK_TIME_SIZE, PEAK_FREQ_SIZE), mode="constant",
                                   cval=-200.0)
        floor = max(float(np.median(db[:, MIN_FREQ_BIN:])) + PEAK_MIN_DB_ABOVE_MEDIAN,
                    PEAK_ABS_FLOOR_DB)
        t, f = np.nonzero((db == local_max) & (db > floor))
        t = t + lo
        own = (t >= b0) & (t < b1)  # drop peaks that belong to a neighbouring block
        times.append(t[own])
        freqs.append(f[own])

    t = np.concatenate(times).astype(np.int32)
    f = np.concatenate(freqs).astype(np.int32)
    order = np.lexsort((f, t))
    return t[order], f[order]


def landmark_hashes(t: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pair each peak with up to FAN_OUT following peaks and hash each pair."""
    n = len(t)
    taken = np.zeros(n, dtype=np.int32)
    hashes, anchors = [], []
    for k in range(1, PAIR_LOOKAHEAD + 1):
        if k >= n:
            break
        i = np.arange(n - k)
        j = i + k
        dt = t[j] - t[i]
        df = f[j] - f[i]
        ok = (dt >= 1) & (dt <= TARGET_MAX_DT) & (np.abs(df) <= TARGET_MAX_DF) & (taken[i] < FAN_OUT)
        if not ok.any():
            # peaks are sorted by time, so once every gap is too wide we are done
            if (dt > TARGET_MAX_DT).all():
                break
            continue
        i_ok = i[ok]
        taken[i_ok] += 1
        h = (f[i_ok].astype(np.uint32) << 15) | ((df[ok] + 128).astype(np.uint32) << 6) | dt[ok].astype(np.uint32)
        hashes.append(h)
        anchors.append(t[i_ok])
    if not hashes:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    return np.concatenate(hashes), np.concatenate(anchors).astype(np.int32)


def fingerprint_samples(samples: np.ndarray) -> Fingerprint:
    t, f = spectral_peaks(samples)
    hashes, times = landmark_hashes(t, f)
    return Fingerprint(hashes, times, len(samples) / SAMPLE_RATE)


def fingerprint_file(path: str | Path) -> Fingerprint:
    return fingerprint_samples(decode_audio(path))
