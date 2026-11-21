"""Final post text per platform, used by the post kit (copy button + clipboard)."""

from __future__ import annotations

import re

from clipper.models import Clip, Job, Platform

YOUTUBE_TITLE_MAX = 100  # YouTube's limit
SHORTS_TAG = "#shorts"


def post_text(job: Job, clip: Clip, platform: Platform) -> str:
    """The caption box text. For YouTube this is the description (the title is separate)."""
    caption = (clip.caption or clip.title or "").strip()
    tags = " ".join(clip.hashtags)
    if platform == Platform.youtube:
        if SHORTS_TAG not in (t.lower() for t in clip.hashtags):
            tags = f"{tags} {SHORTS_TAG}".strip()
    if platform in (Platform.instagram, Platform.youtube):
        text = "\n\n".join(p for p in (caption, tags) if p)
        if job.credit:
            text += f"\n🎥 {job.credit}"
        return text
    # TikTok: one block, pasted into the caption box when posting
    text = " ".join(p for p in (caption, tags) if p)
    if job.credit:
        text += f" 🎥 {job.credit}"
    return text


def post_title(job: Job, clip: Clip) -> str:
    """YouTube Shorts title: Claude's clip title, at most 100 characters, no < or >."""
    raw = clip.title or clip.hook_text or job.title or "Watch till the end"
    title = " ".join(re.sub(r"[<>]", "", raw).split())
    if len(title) <= YOUTUBE_TITLE_MAX:
        return title
    cut = title[: YOUTUBE_TITLE_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"
