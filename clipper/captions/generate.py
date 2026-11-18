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
HASHTAGS_MAX = 5  # per post, base tags included: Instagram's limit, and TikTok's composer's
MAX_SOUNDS_IN_PROMPT = 150  # ~15 tokens each


# ---------------------------------------------------------------- LLM schema


class ClipCaption(BaseModel):
    id: str
    hook_options: list[str]  # drafts; hook_text is the one Claude picked
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
- hook_options: three different on-screen hooks for the first 3 seconds, each at most 6 \
words, no hashtags. It may end with one emoji that fits the moment. A hook opens a \
question the clip answers: tease the outcome \
without giving it away, or name the stakes, or call out who this is for. It must be true \
to the clip, because a hook the clip does not pay off makes viewers leave. Use concrete \
words from the moment, not filler like "wait for it" or "you won't believe this".
- hook_text: the strongest of your three options, copied exactly.
- title: a short internal title (under 60 characters) describing the moment.
- caption: the post caption, at most 150 characters, written like a real fan of this \
niche posts. Name the game or topic in plain words, the way someone would search for it. \
Give viewers a reason to comment or to send it to a friend: a question with more than one \
defensible answer, or a "this is so you" angle. Do not repeat the hook. At most one emoji. \
No hashtags in the caption.
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
            _apply_fallback(clip, job, i + 1)
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
    clip.hook_text = clean_hook(pick_hook(cap.hook_text, cap.hook_options))
    clip.title = cap.title.strip()
    clip.caption = clean_caption(cap.caption)
    clip.hashtags = merge_hashtags(config.BASE_HASHTAGS, cap.hashtags)
    clip.vibe = cap.vibe


def _apply_fallback(clip: Clip, job: Job, number: int) -> None:
    """No Claude: the video title, with a caption of the clip's own so posts don't repeat."""
    title = (job.title or job.context or "Watch till the end").strip()
    # the title up to its first bracket or bar, so six words don't end inside "(THESE LEAKS"
    clip.hook_text = clean_hook(re.split(r"[(\[|]", title)[0].strip() or title)
    clip.title = title
    if clip.score > 0 and clip.reason:  # Claude's ranker said what happens in this clip
        clip.caption = clean_caption(clip.reason)
    else:
        clip.caption = clean_caption(f"{title} (part {number} of {len(job.clips)})")
    clip.hashtags = merge_hashtags(config.BASE_HASHTAGS, [])
    clip.vibe = "hype"


# ---------------------------------------------------------------- normalizers (pure)


EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d]")


def pick_hook(chosen: str, options: list[str]) -> str:
    """Claude's choice, unless it is too long and one of its other drafts fits."""
    fits = [h for h in [chosen, *options] if 0 < _hook_words(h) <= HOOK_MAX_WORDS]
    return fits[0] if fits else chosen


def _hook_words(text: str) -> int:
    return len(EMOJI.sub("", text).split())  # an emoji is not a word


def clean_hook(text: str) -> str:
    """At most HOOK_MAX_WORDS words, no hashtags, and one emoji at the end if there was one."""
    text = re.sub(r"#\w+", "", text)
    emoji = re.search(f"{EMOJI.pattern}+", text)
    words = EMOJI.sub("", text).split()[:HOOK_MAX_WORDS]
    return " ".join(words + ([emoji.group()] if emoji and words else []))


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
    """Base tags first, then new ones, HASHTAGS_MAX in total; deduped, lowercase."""
    out: list[str] = []
    for tag in [*base, *extra]:
        t = normalize_hashtag(tag)
        if t and t not in out and len(out) < HASHTAGS_MAX:
            out.append(t)
    return out
