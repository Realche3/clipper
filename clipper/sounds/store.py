"""Trending sounds on disk: hand-pinned (trending_sounds.yaml) + fetched (trending_sounds.json).

We only ever store song names, never audio. The sound itself is added inside the TikTok /
Instagram editor when posting (ARCHITECTURE.md D12, §5.7).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from clipper import config

log = logging.getLogger(__name__)

VIBES = ("hype", "funny", "chill", "dramatic")


class Sound(BaseModel):
    title: str
    artist: str | None = None
    platform: str = "tiktok"  # tiktok | instagram | both
    vibe: str = "hype"  # hype | funny | chill | dramatic
    source: str | None = None  # page it came from, or "pinned"

    @model_validator(mode="before")
    @classmethod
    def _accept_old_name_key(cls, data):
        # trending_sounds.yaml used `name:` before; keep reading it.
        if isinstance(data, dict) and "title" not in data and "name" in data:
            data = {**data, "title": data["name"]}
        return data

    @property
    def label(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title

    @property
    def key(self) -> tuple[str, str]:
        return (_norm(self.title), _norm(self.artist or ""))


class SourceResult(BaseModel):
    url: str
    count: int = 0
    error: str | None = None


class SoundsFile(BaseModel):
    fetched_at: datetime | None = None
    sources: list[SourceResult] = Field(default_factory=list)
    sounds: list[Sound] = Field(default_factory=list)


def _norm(text: str) -> str:
    text = re.sub(r"['’`]", "", text.lower())  # "Don’t" == "dont"
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def search_url(label: str) -> str:
    """One tap from the dashboard to the sound in TikTok's search."""
    return "https://www.tiktok.com/search?q=" + quote_plus(
        " ".join(label.replace("—", " ").split())
    )


def merge(*lists: list[Sound]) -> list[Sound]:
    """Concatenate, dropping repeats of the same (title, artist). Earlier lists win."""
    seen: set[tuple[str, str]] = set()
    out = []
    for sounds in lists:
        for s in sounds:
            if s.title.strip() and s.key not in seen:
                seen.add(s.key)
                out.append(s)
    return out


# ---------------------------------------------------------------- files


def load_pinned(path: Path | None = None) -> list[Sound]:
    path = path or config.TRENDING_SOUNDS_FILE
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [Sound.model_validate({**s, "source": "pinned"}) for s in data.get("sounds") or []]
    except (yaml.YAMLError, ValidationError, AttributeError, TypeError) as e:
        log.warning("could not read %s (%s); ignoring pinned sounds", path, e)
        return []


def load_fetched(path: Path | None = None) -> SoundsFile | None:
    path = path or config.TRENDING_SOUNDS_JSON
    if not path.exists():
        return None
    try:
        return SoundsFile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as e:
        log.warning("could not read %s (%s); run `clipper sounds refresh`", path, e)
        return None


def save_fetched(data: SoundsFile, path: Path | None = None) -> Path:
    path = path or config.TRENDING_SOUNDS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def all_sounds() -> list[Sound]:
    """Pinned first, then the latest fetched list."""
    fetched = load_fetched()
    return merge(load_pinned(), fetched.sounds if fetched else [])


def pick_sound(sounds: list[Sound], vibe: str | None, index: int = 0) -> Sound | None:
    """Fallback when Claude didn't pick: a sound with the same vibe, rotating by `index` so
    clips from one video don't all get the same one."""
    if not sounds:
        return None
    matches = [s for s in sounds if s.vibe == vibe] or sounds
    return matches[index % len(matches)]
