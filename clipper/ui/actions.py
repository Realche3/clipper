"""Everything the dashboard does, as plain functions (no Streamlit), so it can be tested.

Ownership rule (ARCHITECTURE.md §6): the pipeline owns a manifest while it runs; the
dashboard may only edit a job once it is `rendered`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from clipper import config
from clipper.captions.generate import normalize_hashtag
from clipper.models import Job, JobStatus, Platform, Post, PostStatus, ReviewStatus
from clipper.publish import queue

LOG_NAME = "job.log"
_URL = re.compile(r"^https?://\S+$")


class ActionError(ValueError):
    """Shown to the user as-is."""


@dataclass
class JobRow:
    id: str
    job: Job | None  # None while the pipeline is starting and hasn't saved a manifest yet
    started_at: datetime

    @property
    def status(self) -> str:
        return self.job.status.value if self.job else "starting"

    @property
    def label(self) -> str:
        title = (self.job.title if self.job else None) or (
            self.job.source_url if self.job else "starting..."
        )
        return f"{self.started_at:%b %d %H:%M} · {title} · {self.status}"


# ---------------------------------------------------------------- jobs


def jobs_dir() -> Path:
    return config.WORKSPACE / "jobs"


def submit_job(url: str, context: str | None = None) -> str:
    """Start `clipper run` in the background. Returns the job id."""
    url = url.strip()
    if not _URL.match(url):
        raise ActionError("Paste a full video link starting with https://")
    job_id = uuid.uuid4().hex[:8]
    args = ["run", url, "--job-id", job_id]
    if context and context.strip():
        args += ["--context", context.strip()]
    _spawn(job_id, args, append=False)
    return job_id


def resume_job(job_id: str) -> None:
    """Retry a failed job in the background. Stages skip work that is already done."""
    job = Job.load(config.WORKSPACE, job_id)
    if job.status != JobStatus.failed:
        raise ActionError("Only failed jobs can be retried.")
    _spawn(job_id, ["resume", job_id], append=True)


def _spawn(job_id: str, cli_args: list[str], *, append: bool) -> None:
    job_dir = Job.dir_for(config.WORKSPACE, job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    with open(job_dir / LOG_NAME, "a" if append else "w", encoding="utf-8") as log:
        subprocess.Popen(
            _cli(*cli_args),
            cwd=config.ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=_env(),
            creationflags=_NO_WINDOW,
        )


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "clipper.cli", *args]


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}  # emoji in logs on Windows consoles


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def list_jobs() -> list[JobRow]:
    if not jobs_dir().exists():
        return []
    rows = []
    for d in jobs_dir().iterdir():
        if not d.is_dir():
            continue
        manifest = d / "manifest.json"
        job = None
        if manifest.exists():
            try:
                job = Job.load(config.WORKSPACE, d.name)
            except ValueError:
                job = None  # half-written; next refresh will catch it
        if job is None and not (d / LOG_NAME).exists():
            continue
        started = job.created_at if job else datetime.fromtimestamp(d.stat().st_mtime)
        rows.append(JobRow(id=d.name, job=job, started_at=started))
    return sorted(rows, key=lambda r: r.started_at, reverse=True)


def log_tail(job_id: str, lines: int = 20) -> str:
    path = Job.dir_for(config.WORKSPACE, job_id) / LOG_NAME
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def is_finished(row: JobRow) -> bool:
    return row.job is not None and row.job.status in (JobStatus.rendered, JobStatus.failed)


# ---------------------------------------------------------------- clip review


def _editable(job_id: str) -> Job:
    job = Job.load(config.WORKSPACE, job_id)
    if job.status != JobStatus.rendered:
        raise ActionError("Wait until the clips are rendered before editing them.")
    return job


def parse_hashtags(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\s,]+", text):
        tag = normalize_hashtag(raw) if raw else None
        if tag and tag not in out:
            out.append(tag)
    return out


def save_copy(job_id: str, clip_id: str, caption: str, hashtags: str) -> None:
    """Save edited caption and hashtags. Queued posts pick up the new text when they go out."""
    job = _editable(job_id)
    clip = job.clip(clip_id)
    clip.caption = caption.strip()
    clip.hashtags = parse_hashtags(hashtags)
    job.save(config.WORKSPACE)


def approve(job_id: str, clip_id: str) -> list[Post]:
    job = _editable(job_id)
    clip = job.clip(clip_id)
    previous = clip.review
    clip.review = ReviewStatus.approved
    job.save(config.WORKSPACE)
    conn = queue.connect()
    try:
        return queue.enqueue(conn, job, clip)
    except queue.EnqueueError as e:
        clip.review = previous
        job.save(config.WORKSPACE)
        raise ActionError(str(e)) from e
    finally:
        conn.close()


MIN_CLIP_SECONDS = 3.0  # platform minimum for Reels/TikTok
MAX_CLIP_SECONDS = 180.0
RENDER_TIMEOUT = 600
_LOCKED_STATUSES = {
    PostStatus.uploading,
    PostStatus.uploaded_draft,
    PostStatus.published,
    PostStatus.needs_check,
}


def edit_clip(job_id: str, clip_id: str, hook_text: str, start: float, end: float) -> None:
    """Change the hook and/or trim, then re-render just this clip (blocks ~10-60s).

    Queued posts pick up the new file. Not allowed once the clip has started going out.
    """
    job = _editable(job_id)
    clip = job.clip(clip_id)
    if any(p.status in _LOCKED_STATUSES for p in posts_for_clip(job_id, clip_id)):
        raise ActionError("This clip has already been uploaded; it can't be changed now.")
    if start < 0 or end <= start:
        raise ActionError("End must be after start.")
    if job.duration and end > job.duration + 0.5:
        raise ActionError(f"End is past the end of the video ({job.duration:.0f}s).")
    if not MIN_CLIP_SECONDS <= end - start <= MAX_CLIP_SECONDS:
        raise ActionError(
            f"Clips must be {MIN_CLIP_SECONDS:.0f}-{MAX_CLIP_SECONDS:.0f}s long "
            f"(this one is {end - start:.1f}s)."
        )

    clip.hook_text = hook_text.strip() or None
    clip.start, clip.end = round(start, 2), round(end, 2)
    job.save(config.WORKSPACE)

    result = subprocess.run(
        _cli("stage", "render", job_id, "--clip", clip_id, "--force"),
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        creationflags=_NO_WINDOW,
        timeout=RENDER_TIMEOUT,
    )
    if result.returncode != 0:
        # The CLI marks the whole job failed; one bad re-render shouldn't block the others.
        # The previous clip file is untouched (render writes to a temp file first).
        job = Job.load(config.WORKSPACE, job_id)
        job.status = JobStatus.rendered
        job.save(config.WORKSPACE)
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-5:])
        raise ActionError(f"Re-render failed; the previous version is kept.\n{tail}")


def reject(job_id: str, clip_id: str) -> int:
    """Mark a clip rejected and cancel any of its posts that haven't started. Returns how
    many posts were cancelled."""
    job = _editable(job_id)
    job.clip(clip_id).review = ReviewStatus.rejected
    job.save(config.WORKSPACE)
    conn = queue.connect()
    try:
        cancelled = 0
        for post in queue.list_posts(conn, job_id=job_id):
            if post.clip_id == clip_id and queue.cancel(conn, post.id):
                cancelled += 1
        return cancelled
    finally:
        conn.close()


# ---------------------------------------------------------------- queue


def posts(status: PostStatus | None = None) -> list[Post]:
    conn = queue.connect()
    try:
        return queue.list_posts(conn, status=status)
    finally:
        conn.close()


def posts_for_clip(job_id: str, clip_id: str) -> list[Post]:
    return [p for p in posts() if p.job_id == job_id and p.clip_id == clip_id]


def cancel_post(post_id: int) -> bool:
    conn = queue.connect()
    try:
        return queue.cancel(conn, post_id)
    finally:
        conn.close()


def mark_posted(post_id: int, url: str | None = None) -> None:
    """You finished a TikTok draft in the app, or checked a needs_check post and it's live."""
    conn = queue.connect()
    try:
        if not queue.mark_published(conn, post_id, url=(url or "").strip(), now=datetime.now()):
            raise ActionError("That post can't be marked as posted any more.")
    finally:
        conn.close()


def retry_post(post_id: int) -> Post:
    """You checked the account and the post did NOT go out: schedule it again."""
    conn = queue.connect()
    try:
        post = queue.requeue(conn, post_id, now=datetime.now())
    finally:
        conn.close()
    if post is None:
        raise ActionError("Only failed or needs-check posts can be retried.")
    return post


def give_up_post(post_id: int) -> None:
    conn = queue.connect()
    try:
        if not queue.give_up(conn, post_id, now=datetime.now()):
            raise ActionError("Only needs-check posts can be dropped.")
    finally:
        conn.close()


def problem_count() -> int:
    return len([p for p in posts() if p.status in (PostStatus.failed, PostStatus.needs_check)])


def load_clip(job_id: str, clip_id: str):
    job = Job.load(config.WORKSPACE, job_id)
    return job, job.clip(clip_id)


def scheduler_last_seen() -> datetime | None:
    beat = config.WORKSPACE / "scheduler.heartbeat"
    return datetime.fromtimestamp(beat.stat().st_mtime) if beat.exists() else None


PLATFORM_LABEL = {Platform.tiktok: "TikTok", Platform.instagram: "Instagram"}
