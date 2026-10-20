"""Cut sections out of an episode's audio, with a short fade at each join.

One ffmpeg decodes the episode to raw samples, the kept stretches go through
here with their edges faded, and a second ffmpeg encodes them in the source's
format at about its bitrate. Memory stays the same whatever the length, and
cuts land on the exact sample. Tags and cover art are copied from the
source. The source's chapters are dropped, since their times would be wrong;
the caller passes chapters that fit the cut (see `podblock/app/chapters.py`).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

FADE_SEC = 0.04  # each side of a join
MIN_KEEP_SEC = 1.0  # shorter stretches between two cuts are cut too
CHUNK_FRAMES = 1 << 16

BITRATES = (32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)  # kbit/s


@dataclass
class Output:
    suffix: str
    mime: str
    format: str  # ffmpeg's muxer
    args: list[str]
    cover_art: bool = False


OUTPUTS = {
    "mp3": Output(".mp3", "audio/mpeg", "mp3", ["-c:a", "libmp3lame", "-id3v2_version", "3"], cover_art=True),
    "aac": Output(".m4a", "audio/mp4", "ipod", ["-c:a", "aac", "-movflags", "+faststart"]),
    "opus": Output(".opus", "audio/ogg", "ogg", ["-c:a", "libopus"]),
    "vorbis": Output(".ogg", "audio/ogg", "ogg", ["-c:a", "libvorbis"]),
}


class CutError(Exception):
    pass


@dataclass
class Probe:
    codec: str
    sample_rate: int
    channels: int
    bit_rate: int | None  # bit/s
    duration: float | None


@dataclass
class Rendered:
    path: Path
    mime: str
    duration: float
    size: int


def probe(path: Path) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format",
         "-print_format", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise CutError(f"ffprobe couldn't read {path.name}: {out.stderr.strip()[:200]}")
    info = json.loads(out.stdout)
    if not info.get("streams"):
        raise CutError(f"{path.name} has no audio")
    stream, fmt = info["streams"][0], info.get("format", {})
    rate = stream.get("bit_rate") or fmt.get("bit_rate")
    length = stream.get("duration") or fmt.get("duration")
    return Probe(codec=stream.get("codec_name", ""), sample_rate=int(stream.get("sample_rate") or 44100),
                 channels=min(int(stream.get("channels") or 2), 2),
                 bit_rate=int(rate) if rate and str(rate).isdigit() else None,
                 duration=float(length) if length else None)


def chapters(path: Path) -> list[dict]:
    """The chapters in an audio file: start and end in seconds, and title."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-print_format", "json", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise CutError(f"ffprobe couldn't read {path.name}: {out.stderr.strip()[:200]}")
    found = []
    for c in json.loads(out.stdout or "{}").get("chapters", []):
        try:
            found.append({"start": float(c["start_time"]), "end": float(c["end_time"]),
                          "title": (c.get("tags") or {}).get("title", "").strip()})
        except (KeyError, ValueError):
            continue
    return sorted(found, key=lambda c: c["start"])


def _ffmetadata(chapters: list[dict], length: float) -> str:
    """Chapters as ffmpeg's metadata file: each runs to the next one's start, the last to the end."""
    def esc(text: str) -> str:
        return "".join("\\" + ch if ch in "=;#\\\n" else ch for ch in text)

    lines = [";FFMETADATA1"]
    starts = [round(c["start"] * 1000) for c in chapters] + [round(length * 1000)]
    for c, start, end in zip(chapters, starts, starts[1:]):
        if end > start:
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={esc(c['title'])}"]
    return "\n".join(lines) + "\n"


def keep_spans(cuts: list[tuple[float, float]], duration: float,
               min_keep: float = MIN_KEEP_SEC) -> list[tuple[float, float]]:
    """The stretches of [0, duration] outside every cut, leaving out slivers."""
    spans, cursor = [], 0.0
    for start, end in sorted(cuts):
        start, end = max(0.0, start), min(duration, end)
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if duration > cursor:
        spans.append((cursor, duration))
    return [(a, b) for a, b in spans if b - a >= min_keep]


def _bitrate(source: Probe) -> str:
    if not source.bit_rate:
        return "128k"
    kbps = source.bit_rate / 1000
    return f"{min(BITRATES, key=lambda b: abs(b - kbps))}k"


def _gain(start: int, end: int, span: tuple[int, int], fade: int, fade_in: bool, fade_out: bool) -> np.ndarray | None:
    """Gain for samples [start, end) of a kept span, or None where it is all 1."""
    a, b = span
    idx = np.arange(start, end)
    gain = np.ones(end - start)
    if fade_in and start < a + fade:
        gain = np.minimum(gain, (idx - a + 1) / fade)
    if fade_out and end > b - fade:
        gain = np.minimum(gain, (b - idx) / fade)
    return None if gain.min() >= 1.0 else np.clip(gain, 0.0, 1.0)


def render(source: Path, dest_stem: Path, keep: list[tuple[float, float]],
           progress: Callable[[float], None] | None = None, chapters: list[dict] | None = None) -> Rendered:
    """Write the `keep` stretches of `source`, in order, to `dest_stem` plus a suffix for its format.

    `chapters` ({start, title}, in the cut's time) are written into the file.
    `progress(fraction)` is called as it goes; an exception it raises stops the render.
    """
    info = probe(source)
    out = OUTPUTS.get(info.codec, OUTPUTS["mp3"])
    dest = dest_stem.with_suffix(out.suffix)
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    sr, ch = info.sample_rate, info.channels
    spans = [(round(a * sr), round(b * sr)) for a, b in keep]
    if not spans:
        raise CutError("nothing would be left")
    total = sum(b - a for a, b in spans)
    last_end = round((info.duration or keep[-1][1]) * sr)
    fade = max(1, round(FADE_SEC * sr))

    decode_cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-map", "0:a:0",
                  "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(sr), "-ac", str(ch), "pipe:1"]
    encode_cmd = ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(sr), "-ac", str(ch), "-i", "pipe:0",
                  "-i", str(source)]
    meta = tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) if chapters else None
    if meta:
        with meta:
            meta.write(_ffmetadata(chapters, total / sr))
        encode_cmd += ["-f", "ffmetadata", "-i", meta.name]
    encode_cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "2" if meta else "-1"]
    if out.cover_art:
        encode_cmd += ["-map", "1:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
    encode_cmd += [*out.args, "-b:a", _bitrate(info), "-f", out.format, str(tmp)]

    frame_bytes = 2 * ch
    written = 0
    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        def failure(what: str, log) -> CutError:
            log.seek(0)
            return CutError(f"ffmpeg couldn't {what}: {log.read().decode(errors='replace').strip()[:300]}")

        dec = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=dec_err)
        enc = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE, stderr=enc_err)
        try:
            pos, si, leftover = 0, 0, b""
            while si < len(spans):
                raw = dec.stdout.read(CHUNK_FRAMES * frame_bytes)
                if not raw:
                    break
                raw = leftover + raw
                usable = len(raw) - len(raw) % frame_bytes
                leftover = raw[usable:]
                chunk = np.frombuffer(raw[:usable], "<i2").reshape(-1, ch)
                end_pos = pos + len(chunk)
                while si < len(spans) and spans[si][0] < end_pos:
                    a, b = spans[si]
                    lo, hi = max(a, pos), min(b, end_pos)
                    if hi > lo:
                        piece = chunk[lo - pos:hi - pos]
                        gain = _gain(lo, hi, (a, b), fade, fade_in=a > 0, fade_out=b < last_end - 1)
                        if gain is not None:
                            piece = (piece * gain[:, None]).astype("<i2")
                        try:
                            enc.stdin.write(piece.tobytes())
                        except BrokenPipeError:
                            enc.wait()
                            raise failure("encode", enc_err) from None
                        written += hi - lo
                    if b <= end_pos:
                        si += 1
                    else:
                        break
                pos = end_pos
                if progress:
                    progress(min(written / total, 1.0))
            if written == 0:
                dec.wait()
                raise failure("decode the source", dec_err)
            enc.stdin.close()
            if enc.wait() != 0:
                raise failure("encode", enc_err)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            try:
                enc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
            for proc in (dec, enc):
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            dec.stdout.close()
            if meta:
                Path(meta.name).unlink(missing_ok=True)
    tmp.replace(dest)
    return Rendered(dest, out.mime, written / sr, dest.stat().st_size)


def peaks(path: Path, per_second: int = 10) -> list[int]:
    """How loud each 1/per_second of a second is (its RMS), scaled to 0-255, for drawing a waveform.

    RMS rather than the loudest sample: podcasts are compressed so hard that their peaks are all
    near full scale, and a waveform of peaks is a solid bar."""
    rate = 8000
    step = rate // per_second
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "s16le",
           "-acodec", "pcm_s16le", "-ar", str(rate), "-ac", "1", "pipe:1"]
    found, left = [], np.zeros(0, np.int16)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            while raw := proc.stdout.read(step * 2 * 4096):
                samples = np.concatenate([left, np.frombuffer(raw[:len(raw) - len(raw) % 2], "<i2")])
                whole = len(samples) - len(samples) % step
                if whole:
                    bins = samples[:whole].astype(np.float64).reshape(-1, step)
                    found.append(np.sqrt((bins ** 2).mean(axis=1)))
                left = samples[whole:]
        finally:
            proc.stdout.close()
            proc.wait()
        if proc.returncode != 0 and not found:
            err.seek(0)
            raise CutError(f"ffmpeg couldn't read {path.name}: {err.read().decode(errors='replace').strip()[:200]}")
    if len(left):
        found.append(np.array([np.sqrt((left.astype(np.float64) ** 2).mean())]))
    if not found:
        return []
    # In decibels, from the quiet end of this episode to its loud end: compressed speech sits in a
    # narrow band, and a linear scale draws it as one flat bar.
    loud = 20 * np.log10(np.concatenate(found).astype(float) + 1.0)
    floor, top = np.percentile(loud, 10), np.percentile(loud, 99.8)
    scaled = np.clip((loud - floor) / max(top - floor, 1.0), 0.0, 1.0) ** 1.6
    return (scaled * 245 + 10).astype(np.uint8).tolist()

