"""Final post text per platform, used by the post kit (copy button + clipboard)."""

from __future__ import annotations

from clipper.models import Clip, Job, Platform


def post_text(job: Job, clip: Clip, platform: Platform) -> str:
    caption = (clip.caption or clip.title or "").strip()
    tags = " ".join(clip.hashtags)
    if platform == Platform.instagram:
        parts = [caption]
        if tags:
            parts.append(tags)
        text = "\n\n".join(p for p in parts if p)
        if job.credit:
            text += f"\n🎥 {job.credit}"
        return text
    # TikTok: one block, pasted into the caption box when posting
    text = " ".join(p for p in (caption, tags) if p)
    if job.credit:
        text += f" 🎥 {job.credit}"
    return text
