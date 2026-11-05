"""Posting loop: every tick, flag stuck uploads, then post everything that's due.

Safety rules (CLAUDE.md "Hard rules"):
- A post is claimed (scheduled -> uploading) before the client is called.
- PublishError = nothing went out: retryable -> retried later, otherwise -> failed.
- Any other exception (incl. UploadStateUnknown) means "might have posted" -> needs_check,
  never an automatic retry.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from clipper import config
from clipper.models import Job, Platform, Post, PostStatus
from clipper.publish import queue
from clipper.publish.clients import PublishClient, PublishError
from clipper.publish.slots import parse_window

log = logging.getLogger(__name__)

TICK_SECONDS = 60
RETRY_DELAYS = [timedelta(minutes=m) for m in (5, 15, 45)]  # up to 3 retries, 4 tries total
STUCK_AFTER = timedelta(minutes=15)
MAX_POSTS_PER_TICK = 20
SUCCESS_STATUSES = {PostStatus.published, PostStatus.uploaded_draft}


def run_once(
    conn: sqlite3.Connection,
    clients: dict[Platform, PublishClient],
    now: datetime | None = None,
) -> int:
    """One scheduler tick. Returns how many posts were processed."""
    now = now or datetime.now()
    stuck = queue.mark_stuck(conn, now, STUCK_AFTER)
    if stuck:
        log.warning("%d interrupted upload(s) marked needs_check", stuck)

    processed = 0
    while processed < MAX_POSTS_PER_TICK:
        post = queue.claim_next(conn, now, list(clients))
        if post is None:
            break
        _process(conn, post, clients[post.platform], now)
        processed += 1
    return processed


def _process(conn: sqlite3.Connection, post: Post, client: PublishClient, now: datetime) -> None:
    label = f"post {post.id} ({post.job_id}/{post.clip_id} -> {post.platform.value})"

    # Load the clip. Problems here happen before any upload, so the post is just failed.
    try:
        job = Job.load(config.WORKSPACE, post.job_id)
        clip = job.clip(post.clip_id)
    except (FileNotFoundError, StopIteration, ValueError) as e:
        log.error("%s: can't load clip: %s", label, e)
        queue.record_final(
            conn, post.id, status=PostStatus.failed, error=f"can't load clip: {e}", now=now
        )
        return
    if clip.file is None or not _resolve(clip.file).exists():
        log.error("%s: rendered file is missing (%s)", label, clip.file)
        queue.record_final(
            conn,
            post.id,
            status=PostStatus.failed,
            error=f"rendered file missing: {clip.file}",
            now=now,
        )
        return

    try:
        result = client.publish(job, clip)
    except PublishError as e:
        if e.retryable and post.attempts <= len(RETRY_DELAYS):
            retry_at = _within_window(now + RETRY_DELAYS[post.attempts - 1])
            log.warning("%s failed (%s); retrying at %s", label, e, retry_at)
            queue.record_retry(conn, post.id, error=str(e), retry_at=retry_at, now=now)
        else:
            log.error("%s failed for good: %s", label, e)
            queue.record_final(conn, post.id, status=PostStatus.failed, error=str(e), now=now)
        return
    except Exception as e:  # unknown state: it may have gone out
        log.exception("%s: unexpected error during upload -> needs_check", label)
        queue.record_final(
            conn,
            post.id,
            status=PostStatus.needs_check,
            error=f"unexpected error, check the account before re-posting: {e!r}",
            now=now,
        )
        return

    if result.status not in SUCCESS_STATUSES:
        queue.record_final(
            conn,
            post.id,
            status=PostStatus.needs_check,
            error=result.error or f"client returned {result.status.value}",
            now=now,
        )
        return
    queue.record_success(
        conn, post.id, status=result.status, remote_id=result.remote_id, url=result.url, now=now
    )
    log.info("%s -> %s %s", label, result.status.value, result.url or result.remote_id or "")


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else config.ROOT / path


def _within_window(t: datetime) -> datetime:
    """Push a retry time that falls outside POSTING_WINDOW to the next window start."""
    start, end = parse_window(config.POSTING_WINDOW)
    day_start = datetime.combine(t.date(), start)
    if t < day_start:
        return day_start
    if t > datetime.combine(t.date(), end):
        return day_start + timedelta(days=1)
    return t


def run_forever(
    conn: sqlite3.Connection,
    clients: dict[Platform, PublishClient],
    *,
    tick_seconds: int = TICK_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    mode = "DRY RUN" if config.DRY_RUN else "LIVE"
    log.info(
        "scheduler started (%s) for %s, tick every %ds",
        mode,
        ", ".join(p.value for p in clients),
        tick_seconds,
    )
    while True:
        try:
            run_once(conn, clients)
        except sqlite3.Error:
            log.exception("database error during tick; will retry next tick")
        sleep(tick_seconds)
