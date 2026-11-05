"""SQLite post queue (workspace/clipper.db). Only clipper/publish touches this table.

Double-post guard: UNIQUE(job_id, clip_id, platform). A clip can be queued once per platform,
ever; re-enqueueing returns the existing row instead of creating a new one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from clipper import config
from clipper.models import Clip, Job, Platform, Post, PostStatus, ReviewStatus
from clipper.publish.slots import next_slot, parse_window

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    clip_id      TEXT NOT NULL,
    platform     TEXT NOT NULL,
    status       TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    remote_id    TEXT,
    url          TEXT,
    error        TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (job_id, clip_id, platform)
);
CREATE INDEX IF NOT EXISTS posts_due ON posts (status, scheduled_at);
"""

# Rows that occupy a slot. Failed posts never went out, so their slot is free again.
_SLOT_STATUSES = tuple(s.value for s in PostStatus if s != PostStatus.failed)


class EnqueueError(ValueError):
    """The clip can't be queued (not approved, not rendered, bad platform)."""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we open transactions explicitly (BEGIN IMMEDIATE) where it matters.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # dashboard + scheduler can read/write together
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post.model_validate(dict(row))


def daily_cap(platform: Platform) -> int:
    return {
        Platform.tiktok: config.DAILY_CAP_TIKTOK,
        Platform.instagram: config.DAILY_CAP_INSTAGRAM,
    }[platform]


def enabled_platforms() -> list[Platform]:
    out = []
    for name in config.ENABLED_PLATFORMS:
        name = name.strip().lower()
        if not name:
            continue
        try:
            out.append(Platform(name))
        except ValueError as e:
            raise EnqueueError(f"unknown platform {name!r} in ENABLED_PLATFORMS") from e
    return out


def taken_slots(conn: sqlite3.Connection, platform: Platform) -> list[datetime]:
    marks = ",".join("?" * len(_SLOT_STATUSES))
    rows = conn.execute(
        f"SELECT scheduled_at FROM posts WHERE platform = ? AND status IN ({marks})",
        (platform.value, *_SLOT_STATUSES),
    )
    return [datetime.fromisoformat(r["scheduled_at"]) for r in rows]


def enqueue(
    conn: sqlite3.Connection,
    job: Job,
    clip: Clip,
    *,
    platforms: list[Platform] | None = None,
    now: datetime | None = None,
) -> list[Post]:
    """Queue an approved, rendered clip on each platform at its next free slot.

    Idempotent: a clip already queued on a platform keeps its existing row.
    """
    if clip.review != ReviewStatus.approved and not config.AUTO_APPROVE:
        raise EnqueueError(f"clip {job.id}/{clip.id} is not approved")
    if clip.file is None:
        raise EnqueueError(f"clip {job.id}/{clip.id} has not been rendered")
    platforms = platforms if platforms is not None else enabled_platforms()
    now = now or datetime.now()
    window = parse_window(config.POSTING_WINDOW)
    interval = timedelta(minutes=config.POST_INTERVAL_MINUTES)

    posts: list[Post] = []
    conn.execute("BEGIN IMMEDIATE")  # lock so two enqueues can't grab the same slot
    try:
        for platform in platforms:
            existing = conn.execute(
                "SELECT * FROM posts WHERE job_id = ? AND clip_id = ? AND platform = ?",
                (job.id, clip.id, platform.value),
            ).fetchone()
            if existing:
                posts.append(_row_to_post(existing))
                continue
            slot = next_slot(
                now,
                taken_slots(conn, platform),
                interval=interval,
                window=window,
                daily_cap=daily_cap(platform),
            )
            cur = conn.execute(
                "INSERT INTO posts (job_id, clip_id, platform, status, scheduled_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    clip.id,
                    platform.value,
                    PostStatus.scheduled.value,
                    slot.isoformat(),
                    now.isoformat(),
                ),
            )
            posts.append(get_post(conn, cur.lastrowid))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return posts


def get_post(conn: sqlite3.Connection, post_id: int) -> Post | None:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return _row_to_post(row) if row else None


def list_posts(
    conn: sqlite3.Connection,
    *,
    status: PostStatus | None = None,
    platform: Platform | None = None,
    job_id: str | None = None,
) -> list[Post]:
    where, args = [], []
    if status is not None:
        where.append("status = ?")
        args.append(status.value)
    if platform is not None:
        where.append("platform = ?")
        args.append(platform.value)
    if job_id is not None:
        where.append("job_id = ?")
        args.append(job_id)
    sql = "SELECT * FROM posts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scheduled_at, id"
    return [_row_to_post(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- scheduler side


def claim_next(conn: sqlite3.Connection, now: datetime, platforms: list[Platform]) -> Post | None:
    """Atomically move the earliest due post (on one of `platforms`) from scheduled to
    uploading and return it. One statement, so two schedulers can never claim the same row.
    """
    if not platforms:
        return None
    marks = ",".join("?" * len(platforms))
    row = conn.execute(
        "UPDATE posts SET status = ?, attempts = attempts + 1, updated_at = ?"
        " WHERE id = (SELECT id FROM posts WHERE status = ? AND scheduled_at <= ?"
        f"             AND platform IN ({marks}) ORDER BY scheduled_at, id LIMIT 1)"
        " RETURNING *",
        (
            PostStatus.uploading.value,
            now.isoformat(),
            PostStatus.scheduled.value,
            now.isoformat(),
            *(p.value for p in platforms),
        ),
    ).fetchone()
    return _row_to_post(row) if row else None


def record_success(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    status: PostStatus,
    remote_id: str | None,
    url: str | None,
    now: datetime,
) -> None:
    conn.execute(
        "UPDATE posts SET status = ?, remote_id = ?, url = ?, error = NULL, updated_at = ?"
        " WHERE id = ?",
        (status.value, remote_id, url, now.isoformat(), post_id),
    )


def record_retry(
    conn: sqlite3.Connection, post_id: int, *, error: str, retry_at: datetime, now: datetime
) -> None:
    """The upload definitely didn't happen; put it back in the queue for later."""
    conn.execute(
        "UPDATE posts SET status = ?, scheduled_at = ?, error = ?, updated_at = ? WHERE id = ?",
        (PostStatus.scheduled.value, retry_at.isoformat(), error, now.isoformat(), post_id),
    )


def record_final(
    conn: sqlite3.Connection, post_id: int, *, status: PostStatus, error: str, now: datetime
) -> None:
    """Stop processing this post: `failed` (gave up) or `needs_check` (human must verify)."""
    conn.execute(
        "UPDATE posts SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status.value, error, now.isoformat(), post_id),
    )


def mark_stuck(conn: sqlite3.Connection, now: datetime, older_than: timedelta) -> int:
    """Posts left in `uploading` (e.g. the scheduler crashed mid-upload) become needs_check.

    We can't know whether the upload went through, so a human checks instead of a retry.
    """
    cur = conn.execute(
        "UPDATE posts SET status = ?, error = ?, updated_at = ?"
        " WHERE status = ? AND updated_at < ?",
        (
            PostStatus.needs_check.value,
            "upload was interrupted; check the account before re-posting",
            now.isoformat(),
            PostStatus.uploading.value,
            (now - older_than).isoformat(),
        ),
    )
    return cur.rowcount


def cancel(conn: sqlite3.Connection, post_id: int) -> bool:
    """Remove a post that hasn't started uploading. Returns False if it can't be cancelled."""
    cur = conn.execute(
        "DELETE FROM posts WHERE id = ? AND status = ?", (post_id, PostStatus.scheduled.value)
    )
    return cur.rowcount == 1
