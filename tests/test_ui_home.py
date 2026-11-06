"""Dashboard redesign: Home page, navigation, overview numbers, clip filter."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clipper import config
from clipper.models import Clip, Job, JobStatus, ReviewStatus
from clipper.publish import queue
from clipper.ui import actions

APP = str(Path(__file__).parents[1] / "clipper" / "ui" / "app.py")


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config, "DB_FILE", ws / "clipper.db")
    monkeypatch.setattr(config, "TRENDING_SOUNDS_FILE", tmp_path / "pinned.yaml")
    monkeypatch.setattr(config, "TRENDING_SOUNDS_JSON", ws / "trending_sounds.json")
    monkeypatch.setattr(config, "AUTO_APPROVE", False)
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "instagram"])
    monkeypatch.setattr(config, "POSTING_WINDOW", "00:00-23:59")
    monkeypatch.setattr(config, "POST_INTERVAL_MINUTES", 90)


@pytest.fixture
def job(tmp_path) -> Job:
    f = tmp_path / "c.mp4"
    f.write_bytes(b"fake")
    j = Job(
        id="j1",
        source_url="https://youtu.be/x",
        title="Police chase",
        status=JobStatus.rendered,
        context="a very long note " * 20,
        clips=[Clip(id=f"c0{i}", start=0, end=20, caption="x", file=f) for i in (1, 2, 3)],
    )
    j.save(config.WORKSPACE)
    return j


def run_app(page=None, query=None):
    at = AppTest.from_file(APP, default_timeout=30)
    if page:
        at.session_state["page"] = page
    if query:
        at.query_params["page"] = query
    return at.run()


def test_overview_counts(job):
    actions.approve("j1", "c01")
    o = actions.overview()
    assert (o.to_review, o.running, o.posted_week) == (2, 0, 0)
    assert o.sounds_stale  # never refreshed
    [tt] = [p for p in actions.posts_for_clip("j1", "c01") if p.platform.value == "tiktok"]
    actions.mark_posted(tt.id)
    assert actions.overview().posted_week == 1
    assert actions.overview(datetime.now() + timedelta(days=8)).posted_week == 0


def test_home_is_the_default_page_and_shows_metrics(job):
    at = run_app()
    assert not at.exception
    labels = {m.label: m.value for m in at.metric}
    assert labels["🎞️ Clips to review"] == "3"
    assert any("waiting for review" in md.value for md in at.markdown)


def test_home_todo_button_switches_page(job):
    at = run_app()
    todo = next(b for b in at.button if b.label == "Open Jobs")
    todo.click().run()
    assert not at.exception
    assert at.session_state["page"] == "Jobs"


def test_query_param_opens_a_page(job):
    at = run_app(query="Sounds")
    assert not at.exception
    assert at.session_state["page"] == "Sounds"
    assert run_app(query="nonsense").session_state["page"] == "Home"


def test_jobs_filter_shows_only_matching_clips(job):
    actions.approve("j1", "c01")
    at = run_app("Jobs")
    assert not at.exception
    assert {b.key for b in at.button if b.key and b.key.startswith("ok-")} == {
        "ok-j1-c01",
        "ok-j1-c02",
        "ok-j1-c03",
    }
    at.segmented_control(key="filter-j1").set_value("To review").run()
    keys = {b.key for b in at.button if b.key and b.key.startswith("ok-")}
    assert keys == {"ok-j1-c02", "ok-j1-c03"}
    assert any("…" in c.value for c in at.caption)  # long note is shortened


def test_marking_posted_celebrates(job, monkeypatch):
    actions.approve("j1", "c01")
    conn = queue.connect()
    conn.execute(
        "UPDATE posts SET scheduled_at = ?", ((datetime.now() - timedelta(1)).isoformat(),)
    )
    conn.close()
    [p, *_] = actions.due_now()
    at = run_app("Queue")
    at.button(key=f"posted-{p.id}").click().run()
    assert not at.exception
    assert at.get("balloons")  # 🎉


def test_review_badges_follow_clip_state(job):
    actions.approve("j1", "c01")
    actions.reject("j1", "c02")
    at = run_app("Jobs")
    text = " ".join(m.value for m in at.markdown)
    assert "Approved" in text and "Rejected" in text and "To review" in text
    clip = Job.load(config.WORKSPACE, "j1").clip("c03")
    assert clip.review == ReviewStatus.pending


# ---------------------------------------------------------------- last UI round


def test_set_sound_and_clear(job):
    actions.set_sound("j1", "c01", " Vogue — Madonna ")
    assert Job.load(config.WORKSPACE, "j1").clip("c01").suggested_sound == "Vogue — Madonna"
    actions.set_sound("j1", "c01", None)
    assert Job.load(config.WORKSPACE, "j1").clip("c01").suggested_sound is None


def test_bulk_approve_and_reject(job):
    actions.reject("j1", "c01")
    assert actions.approve_all("j1") == 2  # c02, c03; c01 stays rejected
    reviews = {c.id: c.review for c in Job.load(config.WORKSPACE, "j1").clips}
    assert reviews == {
        "c01": ReviewStatus.rejected,
        "c02": ReviewStatus.approved,
        "c03": ReviewStatus.approved,
    }
    assert actions.reject_rest("j1") == 0


def test_posting_streak(job, monkeypatch):
    from datetime import date

    actions.approve_all("j1")
    conn = queue.connect()
    ids = [r["id"] for r in conn.execute("SELECT id FROM posts ORDER BY id")]
    for post_id, days_ago in zip(ids, (0, 1, 2, 4), strict=False):
        when = (datetime(2026, 11, 19, 12) - timedelta(days=days_ago)).isoformat()
        conn.execute(
            "UPDATE posts SET status='published', updated_at=? WHERE id=?", (when, post_id)
        )
    conn.close()
    assert actions.posting_streak(date(2026, 11, 19)) == 3
    assert actions.posting_streak(date(2026, 11, 20)) == 3  # nothing yet today: still counts
    assert actions.posting_streak(date(2026, 11, 22)) == 0


def test_week_strip_has_seven_days(job):
    actions.approve("j1", "c01")
    strip = actions.week_strip()
    assert len(strip) == 7 and strip[0][0] == datetime.now().date()
    assert sum(len(p) for _, p in strip) == 2  # tiktok + instagram


def test_reveal_clip(job, monkeypatch):
    seen = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
    actions.reveal_clip("j1", "c01")
    assert seen and str(job.clips[0].file) in " ".join(seen[0])


def test_jobs_page_new_controls(job):
    from clipper.sounds.store import Sound, SoundsFile, save_fetched

    save_fetched(SoundsFile(fetched_at=datetime.now(), sounds=[Sound(title="Vogue", artist="M")]))
    at = run_app("Jobs")
    assert not at.exception
    keys = {b.key for b in at.button}
    assert {"preview-j1-c01", "reveal-j1-c01", "use-sound-j1-c01", "approve-all-j1"} <= keys
    assert any("/150" in m.value for m in at.markdown)

    at.selectbox(key="sound-j1-c01").set_value("Vogue — M").run()
    at.button(key="use-sound-j1-c01").click().run()
    assert not at.exception
    assert Job.load(config.WORKSPACE, "j1").clip("c01").suggested_sound == "Vogue — M"

    at.button(key="approve-all-j1").click().run()
    assert not at.exception
    assert all(c.review == ReviewStatus.approved for c in Job.load(config.WORKSPACE, "j1").clips)


def test_preview_dialog_opens(job):
    at = run_app("Jobs")
    at.button(key="preview-j1-c01").click().run()
    assert not at.exception


def test_sounds_surprise_me():
    from clipper.sounds.store import Sound, SoundsFile, save_fetched

    save_fetched(SoundsFile(fetched_at=datetime.now(), sounds=[Sound(title="Vogue", artist="M")]))
    at = run_app("Sounds")
    at.button(key="surprise").click().run()
    assert not at.exception
    assert at.session_state["surprise-pick"] == "Vogue — M"
