"""`clipper sounds refresh`: pull currently trending TikTok / Reels songs from public weekly
roundup pages (TRENDING_SOURCES), have Claude extract them, save trending_sounds.json.

Why roundups: neither platform has a trending-audio API, and TikTok's Creative Center chart
needs a signed browser session (ARCHITECTURE.md D19). One failing source never breaks the
refresh, and if every source fails the previous list is kept.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel

from clipper import config, llm
from clipper.sounds.store import Sound, SoundsFile, SourceResult, load_fetched, merge, save_fetched

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)
MAX_TEXT_CHARS = 150_000  # roundup pages are 15-70k chars of text; this only guards outliers
MAX_PARALLEL = 6


class SoundsError(RuntimeError):
    pass


# ---------------------------------------------------------------- page -> text


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?i)<(br|/p|/li|/h[1-6]|/tr|/div)\b[^>]*>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def fetch_text(url: str) -> str:
    response = httpx.get(
        url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0
    )
    response.raise_for_status()
    text = html_to_text(response.text)
    if len(text) > MAX_TEXT_CHARS:
        log.warning("%s: page text is %d chars; using the first %d", url, len(text), MAX_TEXT_CHARS)
        text = text[:MAX_TEXT_CHARS]
    return text


# ---------------------------------------------------------------- text -> sounds (Claude)


class ExtractedSound(BaseModel):
    title: str
    artist: str | None
    platform: Literal["tiktok", "instagram", "both"]
    vibe: Literal["hype", "funny", "chill", "dramatic"]


class Extracted(BaseModel):
    sounds: list[ExtractedSound]


SYSTEM = """\
You read a web page that lists currently trending songs and sounds for TikTok and/or \
Instagram Reels, and you return that list as data.

- Include every song or sound the page presents as trending right now, in page order.
- title: the song or sound name only. artist: the artist or creator, or null if not given.
- platform: tiktok or instagram if the page or section says so, both if it covers both.
- vibe: the mood a creator would use it for: hype (energetic, action), funny (comedic, \
meme), chill (calm, aesthetic), or dramatic (emotional, tense, epic). Use your knowledge of \
the song when the page doesn't say.
- Skip anything that isn't a trending sound: navigation, ads, related articles, older \
"archive" lists from previous months, and generic advice.
- If the page has no such list, return an empty list."""


def extract(url: str, text: str) -> list[Sound]:
    result = llm.ask_json(SYSTEM, f"Page URL: {url}\n\nPage text:\n{text}", Extracted)
    return [
        Sound(title=s.title.strip(), artist=(s.artist or "").strip() or None,
              platform=s.platform, vibe=s.vibe, source=url)
        for s in result.sounds
        if s.title.strip()
    ]  # fmt: skip


# ---------------------------------------------------------------- refresh


def refresh(
    sources: list[str] | None = None,
    *,
    fetch: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> SoundsFile:
    """Fetch every source, merge, and save. Raises SoundsError (and keeps the previous file)
    if no source produced anything."""
    sources = sources if sources is not None else config.TRENDING_SOURCES
    fetch = fetch or fetch_text

    def one(url: str) -> tuple[SourceResult, list[Sound]]:
        try:
            sounds = extract(url, fetch(url))
        except (httpx.HTTPError, llm.LLMError) as e:
            log.warning("sounds: %s failed: %s", url, e)
            return SourceResult(url=url, error=str(e)[:300]), []
        log.info("sounds: %s -> %d", url, len(sounds))
        return SourceResult(url=url, count=len(sounds)), sounds

    # Each source is a page download + one Claude call; run them side by side.
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        outcomes = list(pool.map(one, sources))  # keeps source order for merging
    results = [r for r, _ in outcomes]
    merged = merge(*(sounds for _, sounds in outcomes))
    if not merged:
        previous = load_fetched()
        kept = f"; keeping the previous {len(previous.sounds)}" if previous else ""
        raise SoundsError(f"no trending sounds found in {len(sources)} source(s){kept}")
    data = SoundsFile(fetched_at=now or datetime.now(), sources=results, sounds=merged)
    save_fetched(data)
    return data
