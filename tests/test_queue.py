from datetime import datetime
from pathlib import Path

import pytest

from clipper import config
from clipper.models import Clip, Job, Platform, PostStatus, ReviewStatus
from clipper.publish import queue

NOW = datetime(2026, 11, 19, 12, 0)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPROVE", False)
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "instagram"])
    monkeypatch.setattr(config, "POSTING_WINDOW", "10:00-23:30")
    monkeypatch.setattr(config, "POST_INTERVAL_MINUTES", 90)
    monkeypatch.setattr(config, "DAILY_CAP_TIKTOK", 6)
    monkeypatch.setattr(config, "DAILY_CAP_INSTAGRAM", 5)


@pytest.fixture
def conn(tmp_path):
    c = queue.connect(tmp_path / "clipper.db")
    yield c
    c.close()


def make_clip(id="c01", approved=True, rendered=True) -> Clip:
    return Clip(
        id=id,
        start=0,
        end=30,
        review=ReviewStatus.approved if approved else ReviewStatus.pending,
        file=Path(f"clips/{id}.mp4") if rendered else None,
    )


JOB = Job(id="j1", source_url="https://youtu.be/x")


def test_enqueue_creates_one_post_per_enabled_platform(conn):
    posts = queue.enqueue(conn, JOB, make_clip(), now=NOW)
    assert {p.platform for p in posts} == {Platform.tiktok, Platform.instagram}
    assert all(p.status == PostStatus.scheduled and p.scheduled_at == NOW for p in posts)
    assert all(p.id is not None for p in posts)


def test_enqueue_is_idempotent(conn):
    first = queue.enqueue(conn, JOB, make_clip(), now=NOW)
    again = queue.enqueue(conn, JOB, make_clip(), now=NOW.replace(hour=15))
    assert [p.id for p in again] == [p.id for p in first]
    assert len(queue.list_posts(conn)) == 2


def test_second_clip_is_spaced_by_interval(conn):
    queue.enqueue(conn, JOB, make_clip("c01"), now=NOW, platforms=[Platform.tiktok])
    [p] = queue.enqueue(conn, JOB, make_clip("c02"), now=NOW, platforms=[Platform.tiktok])
    assert p.scheduled_at == datetime(2026, 11, 19, 13, 30)


def test_platforms_are_scheduled_independently(conn):
    queue.enqueue(conn, JOB, make_clip("c01"), now=NOW, platforms=[Platform.tiktok])
    [ig] = queue.enqueue(conn, JOB, make_clip("c02"), now=NOW, platforms=[Platform.instagram])
    assert ig.scheduled_at == NOW


def test_daily_cap_pushes_to_next_day(conn, monkeypatch):
    monkeypatch.setattr(config, "DAILY_CAP_TIKTOK", 1)
    queue.enqueue(conn, JOB, make_clip("c01"), now=NOW, platforms=[Platform.tiktok])
    [p] = queue.enqueue(conn, JOB, make_clip("c02"), now=NOW, platforms=[Platform.tiktok])
    assert p.scheduled_at == datetime(2026, 11, 20, 10, 0)


def test_failed_post_frees_its_slot(conn):
    [p] = queue.enqueue(conn, JOB, make_clip("c01"), now=NOW, platforms=[Platform.tiktok])
    conn.execute("UPDATE posts SET status = 'failed' WHERE id = ?", (p.id,))
    [p2] = queue.enqueue(conn, JOB, make_clip("c02"), now=NOW, platforms=[Platform.tiktok])
    assert p2.scheduled_at == NOW


def test_rejects_unapproved_and_unrendered(conn):
    with pytest.raises(queue.EnqueueError, match="not approved"):
        queue.enqueue(conn, JOB, make_clip(approved=False), now=NOW)
    with pytest.raises(queue.EnqueueError, match="not been rendered"):
        queue.enqueue(conn, JOB, make_clip(rendered=False), now=NOW)
    assert queue.list_posts(conn) == []


def test_auto_approve_allows_pending_clip(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPROVE", True)
    assert len(queue.enqueue(conn, JOB, make_clip(approved=False), now=NOW)) == 2


def test_unknown_platform_in_config_raises(conn, monkeypatch):
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "myspace"])
    with pytest.raises(queue.EnqueueError, match="myspace"):
        queue.enqueue(conn, JOB, make_clip(), now=NOW)


def test_database_unique_constraint_blocks_duplicates(conn):
    queue.enqueue(conn, JOB, make_clip(), now=NOW, platforms=[Platform.tiktok])
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute(
            "INSERT INTO posts (job_id, clip_id, platform, status, scheduled_at, updated_at)"
            " VALUES ('j1', 'c01', 'tiktok', 'scheduled', '2026-11-19T12:00', '2026-11-19T12:00')"
        )


def test_list_filters_and_cancel(conn):
    queue.enqueue(conn, JOB, make_clip("c01"), now=NOW)
    tiktok = queue.list_posts(conn, platform=Platform.tiktok)
    assert len(tiktok) == 1 and len(queue.list_posts(conn, job_id="nope")) == 0

    assert queue.cancel(conn, tiktok[0].id) is True
    assert queue.get_post(conn, tiktok[0].id) is None

    [ig] = queue.list_posts(conn)
    conn.execute("UPDATE posts SET status = 'uploading' WHERE id = ?", (ig.id,))
    assert queue.cancel(conn, ig.id) is False  # never cancel something mid-upload


def test_data_survives_reconnect(tmp_path):
    db = tmp_path / "clipper.db"
    c = queue.connect(db)
    queue.enqueue(c, JOB, make_clip(), now=NOW)
    c.close()
    c2 = queue.connect(db)
    assert len(queue.list_posts(c2)) == 2
    c2.close()
