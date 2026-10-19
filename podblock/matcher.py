"""Find stretches of shared audio between two sets of landmark hashes.

Matching hashes from the same audio line up at one fixed time offset
(dt = time in B - time in A). We count hash hits per (offset, one-second bin
of A) and keep runs of bins that are densely hit at the same offset.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fingerprint import FRAME_SEC, seconds_to_frames


@dataclass
class MatchParams:
    min_length_sec: float = 10.0  # shortest shared block worth reporting
    bin_sec: float = 1.0  # time resolution of the hit histogram
    min_hits_per_bin: int = 3  # hash hits a bin needs to count as shared
    max_gap_sec: float = 3.0  # quiet gaps a run may bridge (pauses, crossfades)
    dt_tolerance: int = 2  # offsets within this many frames are the same alignment
    max_hash_occurrences: int = 40  # skip hashes this common on either side (silence, tones)


@dataclass
class Run:
    a_start: int  # frames, in A's timeline
    a_end: int
    dt: int  # B time = A time + dt
    hits: int

    @property
    def b_start(self) -> int:
        return self.a_start + self.dt

    @property
    def b_end(self) -> int:
        return self.a_end + self.dt

    @property
    def length_sec(self) -> float:
        return (self.a_end - self.a_start) * FRAME_SEC


def match_hashes(
    h_a: np.ndarray, h_b: np.ndarray, max_occurrences: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return index pairs (ia, ib) where h_a[ia] == h_b[ib]."""
    if len(h_a) == 0 or len(h_b) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)

    order_b = np.argsort(h_b, kind="stable")
    sorted_b = h_b[order_b]
    lo = np.searchsorted(sorted_b, h_a, side="left")
    hi = np.searchsorted(sorted_b, h_a, side="right")
    counts = hi - lo

    # Drop hashes that are too common on either side; they carry no information
    # and blow up the number of pairs.
    uniq_a, inv_a, count_a = np.unique(h_a, return_inverse=True, return_counts=True)
    keep = (counts > 0) & (counts <= max_occurrences) & (count_a[inv_a] <= max_occurrences)
    ia = np.nonzero(keep)[0]
    counts = counts[ia]
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)

    rep_a = np.repeat(ia, counts)
    starts = np.repeat(lo[ia], counts)
    within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    return rep_a, order_b[starts + within]


def _runs_in_bins(active: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """Group active bin indices into [first, last] runs, bridging small gaps."""
    idx = np.nonzero(active)[0]
    if len(idx) == 0:
        return []
    breaks = np.nonzero(np.diff(idx) > max_gap + 1)[0]
    firsts = np.concatenate(([idx[0]], idx[breaks + 1]))
    lasts = np.concatenate((idx[breaks], [idx[-1]]))
    return list(zip(firsts.tolist(), lasts.tolist()))


def find_runs(t_a: np.ndarray, t_b: np.ndarray, params: MatchParams) -> list[Run]:
    """Given matched anchor times, find shared stretches of audio.

    Runs that cover the same part of A (or of B) as a stronger run are dropped,
    which removes echoes such as a looped music bed matching itself one bar off.
    """
    if len(t_a) == 0:
        return []
    t_a = t_a.astype(np.int64)
    dt = t_b.astype(np.int64) - t_a
    bin_frames = max(1, seconds_to_frames(params.bin_sec))
    min_bins = int(np.ceil(params.min_length_sec / params.bin_sec))
    min_hits_total = params.min_hits_per_bin * min_bins
    max_gap = int(round(params.max_gap_sec / params.bin_sec))

    offsets, counts = np.unique(dt, return_counts=True)
    # Merge neighbouring offsets before thresholding, since small timing jitter
    # spreads one alignment over a few adjacent frames.
    tol = params.dt_tolerance
    cum = np.concatenate(([0], np.cumsum(counts)))
    left = np.searchsorted(offsets, offsets - tol, side="left")
    right = np.searchsorted(offsets, offsets + tol, side="right")
    spread = cum[right] - cum[left]
    candidates = np.nonzero(spread >= min_hits_total)[0]
    candidates = candidates[np.argsort(-spread[candidates], kind="stable")]

    order = np.argsort(dt, kind="stable")
    dt_sorted = dt[order]
    done: list[int] = []
    runs: list[Run] = []
    for c in candidates:
        off = int(offsets[c])
        if any(abs(off - d) <= tol for d in done):
            continue
        done.append(off)
        s = np.searchsorted(dt_sorted, off - tol, side="left")
        e = np.searchsorted(dt_sorted, off + tol, side="right")
        ta_sel = t_a[order[s:e]]
        bins = np.bincount(ta_sel // bin_frames)
        for first, last in _runs_in_bins(bins >= params.min_hits_per_bin, max_gap):
            if last - first + 1 < min_bins:
                continue
            in_run = (ta_sel >= first * bin_frames) & (ta_sel < (last + 1) * bin_frames)
            hit_times = ta_sel[in_run]
            a_start, a_end = int(hit_times.min()), int(hit_times.max()) + 1
            if (a_end - a_start) * FRAME_SEC < params.min_length_sec * 0.9:
                continue
            runs.append(Run(a_start, a_end, off, int(in_run.sum())))

    return _drop_overlapping(runs)


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _drop_overlapping(runs: list[Run], max_shared: float = 0.5) -> list[Run]:
    kept: list[Run] = []
    for r in sorted(runs, key=lambda r: -r.hits):
        length = r.a_end - r.a_start
        clash = any(
            _overlap(r.a_start, r.a_end, k.a_start, k.a_end) > max_shared * length
            or _overlap(r.b_start, r.b_end, k.b_start, k.b_end) > max_shared * length
            for k in kept
        )
        if not clash:
            kept.append(r)
    return sorted(kept, key=lambda r: r.a_start)


def common_runs(
    h_a: np.ndarray, t_a: np.ndarray, h_b: np.ndarray, t_b: np.ndarray, params: MatchParams
) -> list[Run]:
    """Shared stretches between fingerprint A and fingerprint B."""
    ia, ib = match_hashes(h_a, h_b, params.max_hash_occurrences)
    return find_runs(t_a[ia], t_b[ib], params)
