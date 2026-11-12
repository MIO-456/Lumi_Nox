"""Song asset/metadata structures; no voices, player or request queue included."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SongAssets:
    master: Path
    vocal: Path
    instrumental: Path
    lyrics: Path
    translation: Path | None = None


@dataclass
class SongRecord:
    id: str
    title: str
    basename: str
    singer: str
    meaning: str
    enabled: bool = False
    duration_sec: float = 0.0
    bpm: float = 0.0
    subtitle_mode: str = "single"
    assets: SongAssets | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SongValidation:
    runnable: bool
    missing: tuple[str, ...]
    warnings: tuple[str, ...] = ()
