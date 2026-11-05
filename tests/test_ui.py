from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clipper import config
from clipper.models import Clip, Job, JobStatus, Platform, PostStatus, ReviewStatus
from clipper.publish import queue
from clipper.ui import actions

APP = str(Path(__file__).parents[1] / "clipper" / "ui" / "app.py")


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config, "DB_FILE", ws / "clipper.db")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "AUTO_APPROVE", False)
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "instagram"])
    monkeypatch.setattr(config, "POSTING_WINDOW", "00:00-23:59")
    monkeypatch.setattr(config, "POST_INTERVAL_MINUTES", 90)


@pytest.fixture
def rendered_job(tmp_path) -> Job:
    f = tmp_path / "c01.mp4"
    f.write_bytes(b"fake")
    job = Job(
        id="j1",
        source_url="https://youtu.be/x",
        title="Police chase",
        status=JobStatus.rendered,
        clips=[
            Clip(id="c01", start=0, end=30, caption="old", hashtags=["#gta6"], file=f),
            Clip(id="c02", start=40, end=70, caption="two", file=f),
        ],
    )
    job.save(config.WORKSPACE)
    return job


# ---------------------------------------------------------------- submit


def test_submit_spawns_pipeline_with_job_id_and_context(monkeypatch):
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned.update(cmd=cmd, **kw)

    monkeypatch.setattr(actions.subprocess, "Popen", fake_popen)
    job_id = actions.submit_job(" https://www.youtube.com/watch?v=abc ", "GTA 6 chase")

    cmd = spawned["cmd"]
    assert cmd[2:5] == ["clipper.cli", "run", "https://www.youtube.com/watch?v=abc"]
    assert cmd[cmd.index("--job-id") + 1] == job_id
    assert cmd[cmd.index("--context") + 1] == "GTA 6 chase"
    assert spawned["env"]["PYTHONIOENCODING"] == "utf-8"
    assert (Job.dir_for(config.WORKSPACE, job_id) / actions.LOG_NAME).exists()
    [row] = actions.list_jobs()
    assert row.id == job_id and row.status == "starting"


@pytest.mark.parametrize("bad", ["", "not a link", "www.youtube.com/watch?v=x"])
def test_submit_rejects_bad_links(monkeypatch, bad):
    monkeypatch.setattr(actions.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(actions.ActionError):
        actions.submit_job(bad)


# ---------------------------------------------------------------- review


def test_approve_saves_review_and_queues_both_platforms(rendered_job):
    posts = actions.approve("j1", "c01")
    assert {p.platform for p in posts} == {Platform.tiktok, Platform.instagram}
    assert Job.load(config.WORKSPACE, "j1").clip("c01").review == ReviewStatus.approved
    assert len(actions.posts_for_clip("j1", "c01")) == 2


def test_approve_reverts_when_enqueue_fails(rendered_job):
    rendered_job.clips[0].file = None
    rendered_job.save(config.WORKSPACE)
    with pytest.raises(actions.ActionError, match="rendered"):
        actions.approve("j1", "c01")
    assert Job.load(config.WORKSPACE, "j1").clip("c01").review == ReviewStatus.pending


def test_reject_cancels_scheduled_posts(rendered_job):
    actions.approve("j1", "c01")
    assert actions.reject("j1", "c01") == 2
    assert actions.posts_for_clip("j1", "c01") == []
    assert Job.load(config.WORKSPACE, "j1").clip("c01").review == ReviewStatus.rejected


def test_reject_leaves_posts_already_uploading(rendered_job):
    actions.approve("j1", "c01")
    conn = queue.connect()
    queue.claim_next(conn, datetime(2100, 1, 1), [Platform.tiktok])
    conn.close()
    assert actions.reject("j1", "c01") == 1
    [left] = actions.posts_for_clip("j1", "c01")
    assert left.status == PostStatus.uploading


def test_save_copy_normalizes_hashtags(rendered_job):
    actions.save_copy("j1", "c01", "  new caption ", "gta6, #GTA6Chase  vicecity #gta6")
    clip = Job.load(config.WORKSPACE, "j1").clip("c01")
    assert clip.caption == "new caption"
    assert clip.hashtags == ["#gta6", "#gta6chase", "#vicecity"]


def test_cannot_edit_while_pipeline_runs(rendered_job):
    rendered_job.status = JobStatus.captioned
    rendered_job.save(config.WORKSPACE)
    with pytest.raises(actions.ActionError, match="rendered"):
        actions.approve("j1", "c01")


def test_log_tail(rendered_job):
    log = Job.dir_for(config.WORKSPACE, "j1") / actions.LOG_NAME
    log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    assert actions.log_tail("j1", 3) == "line 47\nline 48\nline 49"


# ---------------------------------------------------------------- page smoke tests


def run_app(page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    if page:
        at.session_state["page"] = page
    return at.run()


def test_pages_render_empty_workspace():
    for page in ("New video", "Jobs", "Queue"):
        at = run_app(page)
        assert not at.exception, f"{page}: {at.exception}"


def test_jobs_page_shows_clips_and_approve_works(rendered_job):
    at = run_app("Jobs")
    assert not at.exception
    assert at.text_area(key="cap-j1-c01").value == "old"
    at.button(key="ok-j1-c01").click().run()
    assert not at.exception
    assert Job.load(config.WORKSPACE, "j1").clip("c01").review == ReviewStatus.approved


def test_queue_page_shows_tiktok_draft_text(rendered_job):
    actions.approve("j1", "c01")
    conn = queue.connect()
    [tt] = queue.list_posts(conn, platform=Platform.tiktok)
    queue.record_success(
        conn, tt.id, status=PostStatus.uploaded_draft, remote_id="x", url=None, now=datetime.now()
    )
    conn.close()
    at = run_app("Queue")
    assert not at.exception
    assert any("old #gta6" in c.value for c in at.code)


def test_new_video_form_rejects_bad_link():
    at = run_app("New video")
    at.text_input[0].input("nope")
    at.button[0].click().run()
    assert at.error and "https://" in at.error[0].value
