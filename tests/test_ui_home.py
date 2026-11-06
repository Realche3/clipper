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
