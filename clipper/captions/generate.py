"""Captions stage: one Claude call per job writes hook, title, caption, hashtags and vibe
for every clip, and picks a sound from the trending list (clipper/sounds), then we normalize.

Spec: docs/ARCHITECTURE.md §5.3. Never fails the pipeline: any Claude problem -> fallback.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from clipper import config, llm
from clipper.models import Clip, Job, JobStatus, Transcript
from clipper.sounds.store import Sound, all_sounds, pick_sound

log = logging.getLogger(__name__)

Vibe = Literal["hype", "funny", "chill", "dramatic"]

HOOK_MAX_WORDS = 6
CAPTION_MAX_CHARS = 150
EXTRA_HASHTAGS_MAX = 5
MAX_SOUNDS_IN_PROMPT = 150  # ~15 tokens each


# ---------------------------------------------------------------- LLM schema


class ClipCaption(BaseModel):
    id: str
    hook_text: str
    title: str
    caption: str
    hashtags: list[str]
    vibe: Vibe
    sound: int | None  # number from the trending sounds list, or null


class CaptionSet(BaseModel):
    clips: list[ClipCaption]


SYSTEM = """\
You write short-form social copy for an account that posts video clips to TikTok and \
Instagram Reels. Viewers scroll fast; you get about one second to make them stop.

Work out what the video is about (game, topic, people, event) from what you are given, \
in this order of trust: the poster's note, then the source title and description, then \
the clip transcripts. Write in the voice of that niche's community and pick hashtags that \
community actually searches.

For each clip you receive, write:
- hook_text: on-screen text shown for the first 3 seconds. At most 6 words. It must make \
the viewer want to see what happens, and it must be true to the clip. No hashtags, no emojis.
- title: a short internal title (under 60 characters) describing the moment.
- caption: the post caption, at most 150 characters, written like a real fan of this \
niche posts. At most one emoji. No hashtags in the caption. It can ask a question to \
drive comments.
- hashtags: 3 to 5 hashtags specific to this video's niche and this moment (the game or \
topic name plus what happens), not generic tags like #fyp or #viral.{base_tags_rule}
- vibe: one of hype, funny, chill, dramatic, whichever best fits the moment.
- sound: the number of the trending sound from the list below that best fits this clip's \
moment, vibe and audience (it will be added in the TikTok/Instagram editor over the clip). \
Prefer different sounds for different clips. null if the list is empty.

Rules:
- Base everything on what you are given. Do not invent events, names, or facts that are \
not there.
- Every clip from the same video must get a clearly different hook and caption.
- Never claim the clip is official, leaked, or exclusive unless the source says so.
- Return exactly one entry per clip id you are given, using the same ids."""


# ---------------------------------------------------------------- stage entry


def run(job: Job, force: bool = False) -> Job:
    if not job.clips:
        log.warning("job %s has no clips; nothing to caption", job.id)
        job.status = JobStatus.captioned
        return job
    if not force and all(c.caption for c in job.clips):
        log.info("job %s already captioned; skipping (use --force to redo)", job.id)
        return job

    transcript = _load_transcript(job)
    sounds = all_sounds()[:MAX_SOUNDS_IN_PROMPT]
    captions = _ask_claude(job, transcript, sounds)

    for i, clip in enumerate(job.clips):
        cap = captions.get(clip.id)
        if cap is None:
            if captions:  # Claude answered but skipped this clip
                log.warning("no caption from Claude for clip %s; using fallback", clip.id)
            _apply_fallback(clip, job)
        else:
            _apply_caption(clip, cap)
        clip.suggested_sound = _sound_for(cap, sounds, clip.vibe, i)

    job.status = JobStatus.captioned
    return job


# ---------------------------------------------------------------- helpers


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else config.ROOT / path


def _load_transcript(job: Job) -> Transcript | None:
    if job.transcript_file is None:
        return None
    path = _resolve(job.transcript_file)
    if not path.exists():
        log.warning("transcript %s not found; captioning without it", path)
        return None
    return Transcript.model_validate_json(path.read_text(encoding="utf-8"))


def build_prompt(job: Job, transcript: Transcript | None, sounds: list[Sound] | None = None) -> str:
    lines = [
        f"Poster's note: {job.context or '(none)'}",
        f"Source video title: {job.title or 'unknown'}",
        f"Channel: {job.channel or 'unknown'}",
        f"Source description:\n{job.description or '(none)'}",
        "",
        "Clips:",
    ]
    for clip in job.clips:
        text = transcript.text_between(clip.start, clip.end) if transcript else ""
        lines += [
            f"- id: {clip.id}",
            f"  duration: {clip.duration:.0f}s",
            f"  why it was picked: {clip.reason or 'n/a'}",
            f"  transcript: {text or '(no speech)'}",
        ]
    lines += ["", "Trending sounds (pick by number):"]
    lines += [
        f"{i}. {s.label} ({s.platform}, {s.vibe})" for i, s in enumerate(sounds or [], start=1)
    ] or ["(none)"]
    return "\n".join(lines)


def _sound_for(
    cap: ClipCaption | None, sounds: list[Sound], vibe: str | None, i: int
) -> str | None:
    """Claude's pick if it's a valid number from the list, else a vibe-based fallback."""
    if cap is not None and cap.sound is not None and 1 <= cap.sound <= len(sounds):
        return sounds[cap.sound - 1].label
    fallback = pick_sound(sounds, vibe, index=i)
    return fallback.label if fallback else None


def _ask_claude(
    job: Job, transcript: Transcript | None, sounds: list[Sound]
) -> dict[str, ClipCaption]:
    rule = (
        f" Do not repeat these tags, which are added automatically: "
        f"{' '.join(config.BASE_HASHTAGS)}."
        if config.BASE_HASHTAGS
        else ""
    )
    system = SYSTEM.format(base_tags_rule=rule)
    try:
        result = llm.ask_json(
            system,
            build_prompt(job, transcript, sounds),
            CaptionSet,
            model=config.CLAUDE_MODEL_CAPTIONS,
            task="captions",
        )
    except llm.LLMError as e:
        log.warning("captions: Claude failed (%s); using fallback captions", e)
        return {}
    return {c.id: c for c in result.clips}


def _apply_caption(clip: Clip, cap: ClipCaption) -> None:
    clip.hook_text = clean_hook(cap.hook_text)
    clip.title = cap.title.strip()
    clip.caption = clean_caption(cap.caption)
    clip.hashtags = merge_hashtags(config.BASE_HASHTAGS, cap.hashtags)
    clip.vibe = cap.vibe


def _apply_fallback(clip: Clip, job: Job) -> None:
    title = (job.title or job.context or "Watch till the end").strip()
    clip.hook_text = clean_hook(title)
    clip.title = title
    clip.caption = clean_caption(title)
    clip.hashtags = merge_hashtags(config.BASE_HASHTAGS, [])
    clip.vibe = "hype"


# ---------------------------------------------------------------- normalizers (pure)


def clean_hook(text: str) -> str:
    words = re.sub(r"#\w+", "", text).split()
    return " ".join(words[:HOOK_MAX_WORDS])


def clean_caption(text: str) -> str:
    text = " ".join(re.sub(r"#\w+", "", text).split())  # no hashtags, single spaces
    if len(text) <= CAPTION_MAX_CHARS:
        return text
    cut = text[: CAPTION_MAX_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def normalize_hashtag(tag: str) -> str | None:
    body = re.sub(r"[^\w]", "", tag.strip().lstrip("#"))
    return f"#{body.lower()}" if body else None


def merge_hashtags(base: list[str], extra: list[str]) -> list[str]:
    """Base tags first, then up to EXTRA_HASHTAGS_MAX new ones; deduped, lowercase."""
    out: list[str] = []
    for tag in base:
        t = normalize_hashtag(tag)
        if t and t not in out:
            out.append(t)
    added = 0
    for tag in extra:
        t = normalize_hashtag(tag)
        if t and t not in out and added < EXTRA_HASHTAGS_MAX:
            out.append(t)
            added += 1
    return out
