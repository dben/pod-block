"""Where the app keeps each episode's files under the media folder.

Every file is named by the episode's key, a hash of its guid, which makes a
safe file name of any guid. Private feeds name their audio links the same way.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

AUDIO_SUFFIXES = {".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".oga", ".wav", ".flac"}
MIME_SUFFIXES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                 "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/opus": ".opus"}


def key(guid: str) -> str:
    return hashlib.sha1(guid.encode()).hexdigest()[:16]


def digest(path: Path) -> str:
    """A file's SHA-256, to know a download again."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def audio_suffix(url: str | None, mime: str | None) -> str:
    suffix = Path((url or "").split("?", 1)[0]).suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return suffix
    return MIME_SUFFIXES.get((mime or "").split(";")[0].strip().lower(), ".mp3")


@dataclass
class Media:
    root: Path  # the media folder

    def original(self, ep: sqlite3.Row) -> Path:
        """The download."""
        return self.root / "original" / (key(ep["guid"]) + audio_suffix(ep["audio_url"], ep["audio_type"]))

    def cut(self, ep: sqlite3.Row) -> Path:
        """The cut audio, less its suffix, which is the encoder's to pick."""
        return self.root / "cut" / key(ep["guid"])

    def peaks(self, ep: sqlite3.Row) -> Path:
        """The waveform, as JSON."""
        return self.root / "peaks" / f"{key(ep['guid'])}.json"

    def transcripts(self, ep: sqlite3.Row) -> Path:
        """The source's timed transcripts, less their suffix, which is their format."""
        return self.root / "transcripts" / key(ep["guid"])
