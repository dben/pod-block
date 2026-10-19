"""Find an outro read over a music bed at the end of an episode.

Some shows close with minutes of talk (patron thank-yous, announcements) over
a music bed. The talk differs every week and buries the music, so the bed
rarely matches between episodes. What does give it away is the loudness
floor, the level of the quietest moments: in the show it swings as people
pause, laugh and talk over each other, while under a steady bed it barely
moves. The outro is the stretch at the end where the floor stays steady.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import percentile_filter, uniform_filter1d

from .fingerprint import FRAME_SEC

FLOOR_SEC = 3.0  # the floor is the 10th percentile of loudness over this long
WINDOW_SEC = 30.0  # how long the floor must hold still to count as steady
STEADY_DB = 6.0  # ...meaning its swing stays under this
STEADY_SHARE = 0.8  # ...and under this share of the episode's usual swing
MAX_DIP_SEC = 10.0  # brief unsteady moments (a laugh, a sting) are forgiven
END_SKIP_SEC = 20.0  # the last seconds often hold a closing sting
MIN_OUTRO_SEC = 60.0


def _frames(sec: float) -> int:
    return max(1, int(round(sec / FRAME_SEC)))


def music_bed_outro(loudness: np.ndarray) -> float | None:
    """Start, in seconds, of a steady music-bed outro, or None."""
    loudness = loudness.astype(np.float32)
    if len(loudness) < _frames(4 * WINDOW_SEC):
        return None
    floor = percentile_filter(loudness, 10, size=_frames(FLOOR_SEC))
    w = _frames(WINDOW_SEC)
    mean = uniform_filter1d(floor, w)
    swing = np.sqrt(np.maximum(uniform_filter1d(floor ** 2, w) - mean ** 2, 0))
    usual = float(np.median(swing[: int(len(swing) * 0.8)]))
    steady = swing < min(STEADY_DB, STEADY_SHARE * usual)

    i = len(steady) - _frames(END_SKIP_SEC)
    last_steady = i
    while i > 0:
        if steady[i]:
            last_steady = i
        elif last_steady - i > _frames(MAX_DIP_SEC):
            break
        i -= 1
    start = max(0.0, last_steady * FRAME_SEC - WINDOW_SEC / 2)
    duration = len(loudness) * FRAME_SEC
    return start if duration - start >= MIN_OUTRO_SEC else None
