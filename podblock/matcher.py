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
    min_length_sec: float = 1.5  # shortest shared audio worth reporting
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


def find_runs(t_a: np.ndarray, t_b: np.ndarray, params: MatchParams) -> list[Run]:
    """Given matched anchor times, find shared stretches of audio.

    Runs that cover the same part of A as a stronger run are dropped, which
    removes echoes such as a looped music bed matching itself one bar off.
    Overlap in B is allowed: one stretch of B may turn up several times in A
    (the same ad played twice).
    """
    if len(t_a) == 0:
        return []
    t_a = t_a.astype(np.int64)
    dt = t_b.astype(np.int64) - t_a
    bin_frames = max(1, seconds_to_frames(params.bin_sec))
    min_bins = max(1, int(np.ceil(params.min_length_sec / params.bin_sec)))
    max_gap = int(round(params.max_gap_sec / params.bin_sec))
    tol = params.dt_tolerance

    # Count hits per (offset, bin of A) cell.
    bins = t_a // bin_frames
    n_bins = int(bins.max()) + 1
    cells, counts = np.unique((dt - dt.min()) * n_bins + bins, return_counts=True)

    # Small timing jitter spreads one alignment over a few adjacent offsets,
    # so each cell also counts the hits of the same bin at nearby offsets.
    spread = counts.copy()
    for d in range(-tol, tol + 1):
        if d == 0:
            continue
        idx = np.searchsorted(cells, cells + d * n_bins)
        idx_ok = np.minimum(idx, len(cells) - 1)
        found = cells[idx_ok] == cells + d * n_bins
        spread[found] += counts[idx_ok[found]]

    active = spread >= params.min_hits_per_bin
    cells, counts = cells[active], counts[active]
    if len(cells) == 0:
        return []
    offset, cell_bin = cells // n_bins, cells % n_bins

    # Consecutive active cells at one offset form a run, bridging small gaps.
    new_run = np.ones(len(cells), dtype=bool)
    new_run[1:] = (offset[1:] != offset[:-1]) | (cell_bin[1:] - cell_bin[:-1] > max_gap + 1)
    run_id = np.cumsum(new_run) - 1
    first = cell_bin[new_run]
    last = cell_bin[np.append(new_run[1:], True)]
    run_offset = offset[new_run] + dt.min()
    run_hits = np.bincount(run_id, weights=counts)
    long_enough = np.nonzero(last - first + 1 >= min_bins)[0]

    # Strongest first: the true offset beats its jittered neighbours, which
    # then get dropped as overlapping.
    coarse = [
        Run(int(first[i]) * bin_frames, (int(last[i]) + 1) * bin_frames, int(run_offset[i]),
            int(run_hits[i]))
        for i in long_enough
    ]
    coarse = _drop_overlapping(coarse)

    # Tighten each run to the hits it actually contains.
    order = np.argsort(dt, kind="stable")
    dt_sorted = dt[order]
    runs = []
    for r in coarse:
        s = np.searchsorted(dt_sorted, r.dt - tol, side="left")
        e = np.searchsorted(dt_sorted, r.dt + tol, side="right")
        ta_sel = t_a[order[s:e]]
        hit_times = ta_sel[(ta_sel >= r.a_start) & (ta_sel < r.a_end)]
        a_start, a_end = int(hit_times.min()), int(hit_times.max()) + 1
        if (a_end - a_start) * FRAME_SEC >= params.min_length_sec * 0.9:
            runs.append(Run(a_start, a_end, r.dt, len(hit_times)))
    return sorted(runs, key=lambda r: r.a_start)


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _drop_overlapping(runs: list[Run], max_shared: float = 0.5) -> list[Run]:
    kept: list[Run] = []
    for r in sorted(runs, key=lambda r: -r.hits):
        length = r.a_end - r.a_start
        clash = any(
            _overlap(r.a_start, r.a_end, k.a_start, k.a_end) > max_shared * length for k in kept
        )
        if not clash:
            kept.append(r)
    return kept


def common_runs(
    h_a: np.ndarray, t_a: np.ndarray, h_b: np.ndarray, t_b: np.ndarray, params: MatchParams
) -> list[Run]:
    """Shared stretches between fingerprint A and fingerprint B."""
    ia, ib = match_hashes(h_a, h_b, params.max_hash_occurrences)
    return find_runs(t_a[ia], t_b[ib], params)
