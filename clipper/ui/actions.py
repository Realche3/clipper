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
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pyperclip

from clipper import config, llm
from clipper.captions.generate import normalize_hashtag
from clipper.models import Job, JobStatus, Platform, Post, PostStatus, ReviewStatus
from clipper.publish import queue
from clipper.publish.text import post_text
from clipper.sounds.fetch import SoundsError, refresh
from clipper.sounds.store import Sound, all_sounds, load_fetched

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
_LOCKED_STATUSES = {PostStatus.published}  # once it's out, the file must not change


def edit_clip(job_id: str, clip_id: str, hook_text: str, start: float, end: float) -> None:
    """Change the hook and/or trim, then re-render just this clip (blocks ~10-60s).

    Queued posts pick up the new file. Not allowed once the clip has started going out.
    """
    job = _editable(job_id)
    clip = job.clip(clip_id)
    if any(p.status in _LOCKED_STATUSES for p in posts_for_clip(job_id, clip_id)):
        raise ActionError("This clip has already been posted; it can't be changed now.")
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


def set_sound(job_id: str, clip_id: str, label: str | None) -> None:
    """Swap the suggested sound (a label from the trending list), or clear it with None."""
    job = _editable(job_id)
    job.clip(clip_id).suggested_sound = (label or "").strip() or None
    job.save(config.WORKSPACE)


def approve_all(job_id: str) -> int:
    """Approve every clip still waiting for review. Returns how many were approved."""
    job = _editable(job_id)
    pending = [c.id for c in job.clips if c.review == ReviewStatus.pending]
    for clip_id in pending:
        approve(job_id, clip_id)
    return len(pending)


def reject_rest(job_id: str) -> int:
    """Reject every clip still waiting for review. Returns how many were rejected."""
    job = _editable(job_id)
    pending = [c.id for c in job.clips if c.review == ReviewStatus.pending]
    for clip_id in pending:
        reject(job_id, clip_id)
    return len(pending)


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


def due_now(now: datetime | None = None) -> list[Post]:
    now = now or datetime.now()
    return [p for p in posts(PostStatus.scheduled) if p.scheduled_at <= now]


def up_next(now: datetime | None = None) -> list[Post]:
    now = now or datetime.now()
    return [p for p in posts(PostStatus.scheduled) if p.scheduled_at > now]


def cancel_post(post_id: int) -> bool:
    conn = queue.connect()
    try:
        return queue.cancel(conn, post_id)
    finally:
        conn.close()


def mark_posted(post_id: int, url: str | None = None) -> None:
    """You posted it by hand. The optional link is shown on the Posted list."""
    conn = queue.connect()
    try:
        if not queue.mark_published(conn, post_id, url=(url or "").strip(), now=datetime.now()):
            raise ActionError("That post is already marked as posted (or was cancelled).")
    finally:
        conn.close()


def undo_posted(post_id: int) -> None:
    conn = queue.connect()
    try:
        if not queue.unmark_published(conn, post_id, now=datetime.now()):
            raise ActionError("That post isn't marked as posted.")
    finally:
        conn.close()


def load_clip(job_id: str, clip_id: str):
    job = Job.load(config.WORKSPACE, job_id)
    return job, job.clip(clip_id)


# ---------------------------------------------------------------- post kit (ARCHITECTURE §5.6)
# The dashboard runs on your own computer, so these act on this machine directly.


def upload_url(platform: Platform) -> str:
    return {
        Platform.tiktok: config.TIKTOK_UPLOAD_URL,
        Platform.instagram: config.INSTAGRAM_UPLOAD_URL,
    }[platform]


def copy_text(post_id: int) -> str:
    """Copy the platform-formatted caption + hashtags + credit to the clipboard."""
    text = _post_text(post_id)
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as e:
        raise ActionError(f"Couldn't use the clipboard ({e}). Copy the text from the box.") from e
    return text


def reveal_file(post_id: int) -> Path:
    """Open Explorer / Finder with the clip selected, ready to drag into the upload page."""
    return _reveal(_clip_file(post_id))


def reveal_clip(job_id: str, clip_id: str) -> Path:
    """Same as reveal_file, from a clip card (no post needed)."""
    _, clip = load_clip(job_id, clip_id)
    if clip.file is None or not clip.file.exists():
        raise ActionError("The rendered clip file is missing. Re-render it.")
    return _reveal(clip.file)


def _reveal(path: Path) -> Path:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
    return path


def open_upload_page(post_id: int) -> str:
    url = upload_url(_get_post(post_id).platform)
    webbrowser.open_new_tab(url)
    return url


def start_posting(post_id: int) -> None:
    """One click: caption on the clipboard, file shown, upload page opened."""
    _clip_file(post_id)  # fail early, before touching anything, if the file is gone
    copy_text(post_id)
    reveal_file(post_id)
    open_upload_page(post_id)


def _get_post(post_id: int) -> Post:
    conn = queue.connect()
    try:
        post = queue.get_post(conn, post_id)
    finally:
        conn.close()
    if post is None:
        raise ActionError("That post no longer exists.")
    return post


def _post_text(post_id: int) -> str:
    post = _get_post(post_id)
    job, clip = load_clip(post.job_id, post.clip_id)
    return post_text(job, clip, post.platform)


def _clip_file(post_id: int) -> Path:
    post = _get_post(post_id)
    _, clip = load_clip(post.job_id, post.clip_id)
    if clip.file is None or not clip.file.exists():
        raise ActionError("The rendered clip file is missing. Re-render it on the Jobs page.")
    return clip.file


# ---------------------------------------------------------------- trending sounds


def sounds_overview() -> tuple[list[Sound], datetime | None]:
    fetched = load_fetched()
    return all_sounds(), (fetched.fetched_at if fetched else None)


def refresh_sounds() -> int:
    """Fetch the trending lists now (~1 min). Returns how many sounds were saved."""
    try:
        return len(refresh().sounds)
    except SoundsError as e:
        raise ActionError(str(e)) from e


@dataclass
class Overview:
    to_review: int  # clips in rendered jobs still pending review
    due: int
    posted_week: int
    spend_week: float
    running: int  # jobs still in the pipeline
    sounds_stale: bool


SOUNDS_STALE_AFTER = timedelta(days=3)


def overview(now: datetime | None = None) -> Overview:
    now = now or datetime.now()
    rows = list_jobs()
    to_review = sum(
        1
        for r in rows
        if r.job and r.job.status == JobStatus.rendered
        for c in r.job.clips
        if c.review == ReviewStatus.pending
    )
    running = sum(1 for r in rows if not is_finished(r))
    week_ago = now - timedelta(days=7)
    posted_week = sum(1 for p in posts(PostStatus.published) if p.updated_at >= week_ago)
    _, fetched_at = sounds_overview()
    return Overview(
        to_review=to_review,
        due=len(due_now(now)),
        posted_week=posted_week,
        spend_week=llm.spend_since(week_ago),
        running=running,
        sounds_stale=fetched_at is None or now - fetched_at > SOUNDS_STALE_AFTER,
    )


def posting_streak(today: date | None = None) -> int:
    """Days in a row (ending today, or yesterday if nothing yet today) with a post marked."""
    today = today or date.today()
    days = {p.updated_at.date() for p in posts(PostStatus.published)}
    day = today if today in days else today - timedelta(days=1)
    streak = 0
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def week_strip(now: datetime | None = None) -> list[tuple[date, list[Post]]]:
    """The next 7 days (today first) with their scheduled posts, for the Up next calendar."""
    now = now or datetime.now()
    scheduled = posts(PostStatus.scheduled)
    out = []
    for i in range(7):
        day = (now + timedelta(days=i)).date()
        out.append((day, [p for p in scheduled if p.scheduled_at.date() == day]))
    return out


def claude_spend() -> tuple[float, float]:
    """Estimated Claude API spend (today, last 7 days) from workspace/claude_usage.jsonl."""
    rows = llm.load_usage()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return llm.spend_since(today, rows), llm.spend_since(now - timedelta(days=7), rows)


PLATFORM_LABEL = {Platform.tiktok: "TikTok", Platform.instagram: "Instagram"}
