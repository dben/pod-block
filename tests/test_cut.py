import shutil
import subprocess

import numpy as np
import pytest

from podblock.cut import CutError, keep_spans, probe, render

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

SR = 44100


def tone(freq, secs, sr=SR):
    t = np.arange(int(secs * sr)) / sr
    return 0.5 * np.sin(2 * np.pi * freq * t)


def write(path, samples, sr=SR, channels=1, codec_args=("-c:a", "libmp3lame", "-b:a", "96k")):
    pcm = (np.repeat(samples[:, None], channels, axis=1) * 32767).astype("<i2").tobytes()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(sr), "-ac", str(channels),
                    "-i", "pipe:0", "-metadata", "title=Test episode", *codec_args, str(path)],
                   input=pcm, check=True)
    return path


def decode(path, sr=SR):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(sr),
                          "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<i2").astype(float) / 32767


def level(samples, freq, sr=SR):
    spectrum = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), 1 / sr)
    return spectrum[np.abs(freqs - freq) < 5].max() / len(samples)


@pytest.fixture
def episode(tmp_path):
    # 0-5 s "intro" at 1 kHz, 5-20 s show at 440 Hz, 20-25 s "ad" at 1 kHz, 25-30 s show
    audio = np.concatenate([tone(1000, 5), tone(440, 15), tone(1000, 5), tone(440, 5)])
    return write(tmp_path / "episode.mp3", audio)


def test_keep_spans():
    assert keep_spans([(0, 5), (20, 25)], 30) == [(5, 20), (25, 30)]
    assert keep_spans([(20, 25), (3, 6), (5, 8)], 30) == [(0, 3), (8, 20), (25, 30)]
    assert keep_spans([(0, 10), (10.5, 20)], 20) == []  # a half-second sliver goes too
    assert keep_spans([(-1, 4), (28, 40)], 30) == [(4, 28)]
    assert keep_spans([], 30) == [(0, 30)]


def test_render_cuts_the_sections_out(tmp_path, episode):
    seen = []
    out = render(episode, tmp_path / "cut" / "episode", keep_spans([(0, 5), (20, 25)], 30), progress=seen.append)
    assert out.path.suffix == ".mp3" and out.mime == "audio/mpeg"
    assert out.duration == pytest.approx(20, abs=0.05)
    assert out.size == out.path.stat().st_size
    assert seen and seen[-1] == pytest.approx(1.0)

    cut = decode(out.path)
    assert len(cut) / SR == pytest.approx(20, abs=0.1)
    assert level(cut, 440) > 50 * level(cut, 1000)  # only the show is left
    info = probe(out.path)
    assert info.codec == "mp3" and 80_000 <= info.bit_rate <= 112_000  # about the source's bitrate
    tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title", "-of", "csv=p=0",
                           str(out.path)], capture_output=True, text=True).stdout.strip()
    assert tags == "Test episode"


def test_joins_fade_instead_of_clicking(tmp_path, episode):
    out = render(episode, tmp_path / "cut", [(5, 20), (25, 30)])
    cut = decode(out.path)
    join = int(15 * SR)  # where 20 s met 25 s
    near = np.abs(cut[join - 200:join + 200]).max()
    away = np.abs(cut[join - int(0.5 * SR):join - int(0.4 * SR)]).max()
    assert near < 0.6 * away


def test_aac_stays_aac(tmp_path):
    src = write(tmp_path / "ep.m4a", tone(440, 10), channels=2, codec_args=("-c:a", "aac", "-b:a", "64k"))
    out = render(src, tmp_path / "out", [(2, 8)])
    assert out.path.suffix == ".m4a" and out.mime == "audio/mp4"
    assert out.duration == pytest.approx(6, abs=0.05)
    assert probe(out.path).channels == 2


def test_bad_input(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all" * 100)
    with pytest.raises(CutError):
        render(junk, tmp_path / "out", [(0, 5)])
    assert not list(tmp_path.glob("out*"))
