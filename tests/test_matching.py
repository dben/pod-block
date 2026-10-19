import shutil
import subprocess

import numpy as np
import pytest

from podblock.fingerprint import SAMPLE_RATE, decode_audio, fingerprint_samples, frames_to_seconds
from podblock.matcher import MatchParams, common_runs
from podblock.pipeline import _cut_points
from podblock.store import Store


def fake_audio(seconds: float, seed: int) -> np.ndarray:
    """Noise plus a random melody, which gives the peak picker plenty to grab."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    out = rng.normal(0, 0.02, n)
    note = int(0.12 * SAMPLE_RATE)
    t = np.arange(note) / SAMPLE_RATE
    for start in range(0, n - note, note):
        for freq in rng.uniform(150, 3500, size=3):
            out[start:start + note] += 0.1 * np.sin(2 * np.pi * freq * t) * np.hanning(note)
    return out.astype(np.float32)


@pytest.fixture(scope="module")
def jingle():
    return fake_audio(40, seed=99)


def episode(parts):
    return np.concatenate(parts)


def test_finds_shared_block_at_different_positions(jingle):
    a = episode([fake_audio(60, 1), jingle, fake_audio(50, 2)])
    b = episode([fake_audio(15, 3), jingle, fake_audio(90, 4)])
    fa, fb = fingerprint_samples(a), fingerprint_samples(b)

    runs = common_runs(fa.hashes, fa.times, fb.hashes, fb.times, MatchParams())

    assert len(runs) == 1
    run = runs[0]
    assert frames_to_seconds(run.a_start) == pytest.approx(60, abs=2)
    assert frames_to_seconds(run.a_end) == pytest.approx(100, abs=2)
    assert frames_to_seconds(run.b_start) == pytest.approx(15, abs=2)


def test_unrelated_audio_has_no_shared_blocks():
    fa, fb = fingerprint_samples(fake_audio(120, 5)), fingerprint_samples(fake_audio(120, 6))
    assert common_runs(fa.hashes, fa.times, fb.hashes, fb.times, MatchParams()) == []


def test_store_recognises_block_in_new_audio(tmp_path, jingle):
    a = episode([fake_audio(30, 7), jingle, fake_audio(30, 8)])
    fa = fingerprint_samples(a)
    store = Store(tmp_path / "db.sqlite")
    seg_id = store.add_segment(fa.slice(30, 70), None, 30, 70)

    # A new episode that carries only the first half of the block, with noise added.
    rng = np.random.default_rng(0)
    half = jingle[: 20 * SAMPLE_RATE] + rng.normal(0, 0.03, 20 * SAMPLE_RATE).astype(np.float32)
    c = episode([fake_audio(100, 9), half, fake_audio(20, 10)])
    matches = store.scan(fingerprint_samples(c), MatchParams())

    assert [m.segment_id for m in matches] == [seg_id]
    assert matches[0].start == pytest.approx(100, abs=2)
    assert matches[0].end == pytest.approx(120, abs=2)
    assert matches[0].segment_start == pytest.approx(0, abs=2)


def test_uncovered_returns_only_new_audio(tmp_path, jingle):
    fa = fingerprint_samples(episode([jingle, fake_audio(30, 11)]))
    store = Store(tmp_path / "db.sqlite")
    store.add_segment(fa.slice(0, 40), None, 0, 40)

    gaps = [g for g in store.uncovered(fa, MatchParams()) if g[1] - g[0] > 1]

    assert len(gaps) == 1
    assert gaps[0][0] == pytest.approx(40, abs=2)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_survives_mp3_encoding(tmp_path, jingle):
    def to_mp3(samples, name, bitrate):
        wav = tmp_path / f"{name}.raw"
        wav.write_bytes((samples * 32767).astype(np.int16).tobytes())
        mp3 = tmp_path / f"{name}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                        "-i", str(wav), "-ar", "44100", "-b:a", bitrate, str(mp3)], check=True)
        return decode_audio(mp3)

    a = to_mp3(episode([fake_audio(20, 12), jingle, fake_audio(20, 13)]), "a", "128k")
    b = to_mp3(episode([fake_audio(45, 14), jingle, fake_audio(10, 15)]), "b", "48k")
    fa, fb = fingerprint_samples(a), fingerprint_samples(b)

    runs = common_runs(fa.hashes, fa.times, fb.hashes, fb.times, MatchParams())

    assert len(runs) == 1
    assert runs[0].length_sec == pytest.approx(40, abs=3)


def test_scan_finds_every_repeat_of_a_block(tmp_path, jingle):
    fa = fingerprint_samples(episode([jingle, fake_audio(10, 16)]))
    store = Store(tmp_path / "db.sqlite")
    seg_id = store.add_segment(fa.slice(0, 40), None, 0, 40)

    c = episode([fake_audio(30, 17), jingle, fake_audio(60, 18), jingle])
    matches = store.scan(fingerprint_samples(c), MatchParams())

    assert [m.segment_id for m in matches] == [seg_id, seg_id]
    assert [round(m.start) for m in matches] == pytest.approx([30, 130], abs=2)


def test_cut_points_keep_pieces_long_enough():
    # Edges at 0/40/41 and 75 split a 100 s block into 0-40.5, 40.5-75, 75-100;
    # the edge at 95 would leave a 5 s piece, so it is ignored.
    edges = [0, 40, 41, 75, 95, 100]
    assert _cut_points(edges, 100, 10) == pytest.approx([40.5, 75])
