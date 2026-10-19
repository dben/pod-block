"""Landmark audio fingerprints.

Audio is decoded to mono, turned into a log-magnitude spectrogram and reduced
to a sparse set of spectral peaks. Each peak (the "anchor") is paired with a
few peaks that follow it closely in time, and each pair is packed into a
32-bit hash of (band, anchor freq, freq delta, time delta). Two recordings of
the same audio share many hashes at a constant time offset, even after lossy
re-encoding, volume changes or added noise. That constant offset is what the
matcher looks for.

Two settings change what the fingerprint can see:

- `high_band` decodes at 16 kHz and fingerprints 4-8 kHz as a band of its
  own. Speech sits mostly below 4 kHz, so music played under a voice often
  only shows clearly up there.
- `split_bands` picks peaks within each of several bands separately, with
  each band's own loudness floor, so loud speech in one band cannot crowd out
  quieter music in another.

Every setting uses 32 ms frames and 15.6 Hz bins, so times line up across them,
but hashes from different settings never match: a store sticks to one.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import maximum_filter

FRAME_SEC = 0.032
BIN_HZ = 15.625

# Peak picking: a bin is a peak when it is the largest value within this many
# frames / bins of itself (inside its band) and loud enough to stand out.
PEAK_TIME_SIZE = 21
PEAK_FREQ_SIZE = 21
PEAK_MIN_DB_ABOVE_MEDIAN = 10.0
PEAK_ABS_FLOOR_DB = -100.0  # dB relative to full scale; drops digital silence

# Pairing: each anchor is paired with up to FAN_OUT later peaks in its band.
FAN_OUT = 5
TARGET_MAX_DT = 63  # frames, fits in 6 bits (~2 s)
TARGET_MAX_DF = 127  # bins, fits in 8 bits as a signed value
PAIR_LOOKAHEAD = 40

BLOCK_FRAMES = 8192  # spectrogram is built in blocks of ~4.4 minutes

MAIN_BAND_HZ = (80, 4000)
SPLIT_EDGES_HZ = (80, 300, 1000, 2000, 4000)
HIGH_BAND_HZ = (4000, 8000)


@dataclass(frozen=True)
class FingerprintConfig:
    # On five test episodes the high band nearly tripled how much music under
    # speech was found; splitting bands added under a second, so it is off.
    high_band: bool = True
    split_bands: bool = False

    @property
    def sample_rate(self) -> int:
        return 16000 if self.high_band else 8000

    @property
    def n_fft(self) -> int:
        return int(self.sample_rate / BIN_HZ)

    @property
    def hop(self) -> int:
        return int(round(self.sample_rate * FRAME_SEC))

    def bands(self) -> list[tuple[int, int]]:
        """Frequency bands as [lo, hi) bin ranges."""
        edges = SPLIT_EDGES_HZ if self.split_bands else MAIN_BAND_HZ
        ranges = list(zip(edges, edges[1:]))
        if self.high_band:
            ranges.append(HIGH_BAND_HZ)
        return [(int(lo / BIN_HZ), int(hi / BIN_HZ)) for lo, hi in ranges]

    @property
    def name(self) -> str:
        parts = [p for p, on in (("high", self.high_band), ("split", self.split_bands)) if on]
        return "+".join(parts) or "basic"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "FingerprintConfig":
        return cls(**json.loads(text))


@dataclass
class Fingerprint:
    hashes: np.ndarray  # uint32
    times: np.ndarray  # int32, anchor frame index
    duration: float  # seconds
    config: FingerprintConfig | None = None
    loudness: np.ndarray | None = None  # float16 dB per frame, for spotting music beds

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = {"loudness": self.loudness} if self.loudness is not None else {}
        np.savez_compressed(
            path, hashes=self.hashes, times=self.times, duration=np.float64(self.duration),
            config=np.array(self.config.to_json() if self.config else ""), **extra,
        )

    @classmethod
    def load(cls, path: Path) -> "Fingerprint":
        with np.load(path) as data:
            config = str(data["config"]) if "config" in data else ""
            return cls(data["hashes"], data["times"], float(data["duration"]),
                       FingerprintConfig.from_json(config) if config else None,
                       data["loudness"] if "loudness" in data else None)

    def slice(self, start_sec: float, end_sec: float) -> "Fingerprint":
        """Hashes whose anchor falls inside [start, end), re-timed to start at 0."""
        lo, hi = seconds_to_frames(start_sec), seconds_to_frames(end_sec)
        keep = (self.times >= lo) & (self.times < hi)
        return Fingerprint(self.hashes[keep], self.times[keep] - lo, end_sec - start_sec, self.config)


def seconds_to_frames(sec: float) -> int:
    return int(round(sec / FRAME_SEC))


def frames_to_seconds(frames) -> float:
    return float(frames) * FRAME_SEC


def decode_audio(path: str | Path, sample_rate: int) -> np.ndarray:
    """Decode any format ffmpeg understands into mono int16 samples, all at once."""
    return np.concatenate([np.zeros(0, np.int16), *decode_chunks(path, sample_rate)])


def decode_chunks(path: str | Path, sample_rate: int, chunk_samples: int = 1 << 20):
    """Decode into mono int16 samples, yielded a chunk at a time so memory stays flat."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while data := proc.stdout.read(chunk_samples * 2):
            yield np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
    finally:
        proc.stdout.close()
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed on {path}: {err.decode(errors='replace')}")


def _as_float(samples: np.ndarray) -> np.ndarray:
    return samples if samples.dtype == np.float32 else samples.astype(np.float32) / 32768.0


def _pick_peaks(frames: np.ndarray, first: int, keep: tuple[int, int], config: FingerprintConfig,
                found: list[list[tuple[np.ndarray, np.ndarray]]]) -> None:
    """Peak-pick spectrogram frames numbered from `first`; keep peaks in frames [keep)."""
    window = np.hanning(config.n_fft).astype(np.float32)
    scale = window.sum() / 2  # a full-scale sine reads 0 dB
    spec = np.abs(np.fft.rfft(frames * window, axis=1)) / scale
    db = (20.0 * np.log10(spec + 1e-9)).astype(np.float32)
    for i, (f_lo, f_hi) in enumerate(config.bands()):
        band = db[:, f_lo:f_hi]
        local_max = maximum_filter(band, size=(PEAK_TIME_SIZE, min(PEAK_FREQ_SIZE, f_hi - f_lo)),
                                   mode="constant", cval=-200.0)
        floor = max(float(np.median(band)) + PEAK_MIN_DB_ABOVE_MEDIAN, PEAK_ABS_FLOOR_DB)
        t, f = np.nonzero((band == local_max) & (band > floor))
        t = t + first
        own = (t >= keep[0]) & (t < keep[1])  # drop peaks that belong to a neighbouring block
        found[i].append((t[own], f[own]))


def spectral_peaks(chunks, config: FingerprintConfig) -> tuple[list[tuple[np.ndarray, np.ndarray]], int]:
    """Per band, the (frame index, bin within band) of each peak, sorted by time.

    `chunks` yields samples at `config.sample_rate`, in pieces of any size (or
    pass one array). The spectrogram is built BLOCK_FRAMES frames at a time,
    each with a margin of neighbouring frames for the peak filter, and only
    the samples the next block needs are kept. Returns the peaks and the
    number of samples read.
    """
    if isinstance(chunks, np.ndarray):
        chunks = [chunks]
    n_fft, hop = config.n_fft, config.hop
    margin = PEAK_TIME_SIZE // 2 + 1
    found: list[list[tuple[np.ndarray, np.ndarray]]] = [[] for _ in config.bands()]
    source = iter(chunks)
    buf, buf_start, total, done = np.zeros(0, np.float32), 0, 0, False  # buf_start: frame of buf[0]
    b0 = 0
    while True:
        need = (b0 + BLOCK_FRAMES + margin - 1) * hop + n_fft  # samples, from the start of the audio
        pieces = [buf]
        have = buf_start * hop + len(buf)
        while not done and have < need:
            chunk = next(source, None)
            if chunk is None:
                done = True
            else:
                pieces.append(_as_float(chunk))
                have += len(chunk)
                total += len(chunk)
        buf = np.concatenate(pieces) if len(pieces) > 1 else buf
        available = 1 + (have - n_fft) // hop if have >= n_fft else 0
        b1 = min(b0 + BLOCK_FRAMES, available) if done else b0 + BLOCK_FRAMES
        if b0 >= b1:
            break
        lo, hi = max(0, b0 - margin), min(available, b1 + margin)
        frames = np.lib.stride_tricks.sliding_window_view(buf, n_fft)[::hop]
        _pick_peaks(frames[lo - buf_start:hi - buf_start], lo, (b0, b1), config, found)
        b0 = b1
        cut = max(0, b0 - margin) - buf_start  # frames the next block no longer needs
        buf, buf_start = buf[cut * hop:], buf_start + cut

    peaks = []
    for parts in found:
        t = np.concatenate([np.zeros(0, np.int64), *[p[0] for p in parts]]).astype(np.int32)
        f = np.concatenate([np.zeros(0, np.int64), *[p[1] for p in parts]]).astype(np.int32)
        order = np.lexsort((f, t))
        peaks.append((t[order], f[order]))
    return peaks, total


def landmark_hashes(t: np.ndarray, f: np.ndarray, band: int = 0) -> tuple[np.ndarray, np.ndarray]:
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
        h = ((np.uint32(band) << 23) | (f[i_ok].astype(np.uint32) << 14)
             | ((df[ok] + 128).astype(np.uint32) << 6) | dt[ok].astype(np.uint32))
        hashes.append(h)
        anchors.append(t[i_ok])
    if not hashes:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    return np.concatenate(hashes), np.concatenate(anchors).astype(np.int32)


def loudness_curve(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Loudness in dB of each 32 ms frame, as float16."""
    samples = _as_float(samples)
    hop = int(round(sample_rate * FRAME_SEC))
    n = len(samples) // hop
    power = (samples[:n * hop].reshape(n, hop) ** 2).mean(axis=1)
    return (10.0 * np.log10(power + 1e-12)).astype(np.float16)


class _LoudnessTap:
    """Passes chunks through while building their loudness curve."""

    def __init__(self, chunks, sample_rate: int):
        self.chunks, self.rate = chunks, sample_rate
        self.hop = int(round(sample_rate * FRAME_SEC))
        self.parts: list[np.ndarray] = []
        self.carry = np.zeros(0, np.float32)

    def __iter__(self):
        for chunk in self.chunks:
            joined = np.concatenate([self.carry, _as_float(chunk)])
            whole = len(joined) // self.hop * self.hop
            self.parts.append(loudness_curve(joined[:whole], self.rate))
            self.carry = joined[whole:]
            yield chunk

    def curve(self) -> np.ndarray:
        return np.concatenate([np.zeros(0, np.float16), *self.parts])


def loudness_file(path: str | Path, sample_rate: int) -> np.ndarray:
    """The loudness curve of a file, decoded a chunk at a time."""
    tap = _LoudnessTap(decode_chunks(path, sample_rate), sample_rate)
    for _ in tap:
        pass
    return tap.curve()


def fingerprint_chunks(chunks, config: FingerprintConfig) -> Fingerprint:
    """Fingerprint audio given as chunks of samples at `config.sample_rate`."""
    tap = _LoudnessTap([chunks] if isinstance(chunks, np.ndarray) else chunks, config.sample_rate)
    peaks, total = spectral_peaks(tap, config)
    hashes, times = [], []
    for band, (t, f) in enumerate(peaks):
        h, a = landmark_hashes(t, f, band)
        hashes.append(h)
        times.append(a)
    return Fingerprint(np.concatenate(hashes), np.concatenate(times),
                       total / config.sample_rate, config, tap.curve())


def fingerprint_samples(samples: np.ndarray, config: FingerprintConfig) -> Fingerprint:
    """Fingerprint samples already at `config.sample_rate`."""
    return fingerprint_chunks(samples, config)


def fingerprint_file(path: str | Path, config: FingerprintConfig) -> Fingerprint:
    return fingerprint_chunks(decode_chunks(path, config.sample_rate), config)
