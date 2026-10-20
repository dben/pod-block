"""Settings for the web app and the worker, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .media import Media


JOBS = ("process", "threads", "none")


def _choice(name: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip().lower() or choices[0]
    if value not in choices:
        raise ValueError(f"{name} should be one of {', '.join(choices)}, not {value!r}")
    return value


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path  # podblock.sqlite and fingerprints
    media_dir: Path  # original and cut episode audio
    public_url: str | None = None  # base for links in the private feed; else the request's host
    api_key: str | None = None  # fixed login key; else one is made on first start
    secret_key: str | None = None  # signs session cookies; else one is made and stored
    secure_cookies: bool = False  # send the session cookie over HTTPS only
    background: bool = True  # refresh feeds, clean up and run jobs at all
    jobs: str = "process"  # where: a child process (`podblock worker`), threads of this one, or "none" (run
    #                        `podblock worker` yourself)
    workers: int = 1  # threads running jobs

    @property
    def db_path(self) -> Path:
        return self.data_dir / "podblock.sqlite"

    @property
    def media(self) -> Media:
        """Where each episode's files go under `media_dir`."""
        return Media(self.media_dir)

    @classmethod
    def from_env(cls, data_dir: Path | None = None, media_dir: Path | None = None) -> "Settings":
        data_dir = data_dir or Path(os.environ.get("PODBLOCK_DATA_DIR", "podblock-data"))
        media_dir = media_dir or Path(os.environ.get("PODBLOCK_MEDIA_DIR") or data_dir / "media")
        return cls(
            data_dir=data_dir,
            media_dir=media_dir,
            public_url=(os.environ.get("PODBLOCK_PUBLIC_URL") or "").rstrip("/") or None,
            api_key=os.environ.get("PODBLOCK_API_KEY") or None,
            secret_key=os.environ.get("PODBLOCK_SECRET_KEY") or None,
            secure_cookies=_flag("PODBLOCK_SECURE_COOKIES"),
            workers=int(os.environ.get("PODBLOCK_WORKERS") or 1),
            jobs=_choice("PODBLOCK_JOBS", JOBS),
        )
