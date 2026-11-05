"""B8: clip edits with re-render, retrying failed jobs, resolving posts by hand."""

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from clipper import cli, config
from clipper.models import Clip, Job, JobStatus, Platform, PostStatus
from clipper.publish import queue
from clipper.ui import actions

APP = str(Path(__file__).parents[1] / "clipper" / "ui" / "app.py")
NOW = datetime(2026, 11, 19, 12, 0)


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setenv("WORKSPACE", str(ws))  # for subprocesses
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config, "DB_FILE", ws / "clipper.db")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "AUTO_APPROVE", False)
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "instagram"])
    monkeypatch.setattr(config, "POSTING_WINDOW", "00:00-23:59")
    monkeypatch.setattr(config, "POST_INTERVAL_MINUTES", 90)


@pytest.fixture
def job(tmp_path) -> Job:
    f = tmp_path / "c01.mp4"
    f.write_bytes(b"fake")
    j = Job(
        id="j1",
        source_url="https://youtu.be/x",
        status=JobStatus.rendered,
        duration=120.0,
        clips=[Clip(id="c01", start=10, end=40, hook_text="old hook", caption="cap", file=f)],
    )
    j.save(config.WORKSPACE)
    return j


def fake_run(monkeypatch, returncode=0, stdout="", stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(actions.subprocess, "run", run)
    return calls


def set_status(post_id, status):
    conn = queue.connect()
    conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status.value, post_id))
    conn.close()


# ---------------------------------------------------------------- edit + re-render


def test_edit_saves_and_rerenders_only_that_clip(monkeypatch, job):
    calls = fake_run(monkeypatch)
    actions.edit_clip("j1", "c01", "  New hook ", 12.0, 35.5)
    clip = Job.load(config.WORKSPACE, "j1").clip("c01")
    assert (clip.hook_text, clip.start, clip.end) == ("New hook", 12.0, 35.5)
    cmd = calls[0][0]
    assert cmd[-6:] == ["stage", "render", "j1", "--clip", "c01", "--force"]


def test_empty_hook_removes_it(monkeypatch, job):
    fake_run(monkeypatch)
    actions.edit_clip("j1", "c01", "   ", 10, 40)
    assert Job.load(config.WORKSPACE, "j1").clip("c01").hook_text is None


@pytest.mark.parametrize(
    "start,end,msg",
    [(30, 20, "after start"), (-1, 20, "after start"), (100, 130, "past the end"), (10, 11, "3")],
)
def test_edit_validates_times(monkeypatch, job, start, end, msg):
    calls = fake_run(monkeypatch)
    with pytest.raises(actions.ActionError, match=msg):
        actions.edit_clip("j1", "c01", "h", start, end)
    assert calls == []
    assert Job.load(config.WORKSPACE, "j1").clip("c01").start == 10  # unchanged


def test_failed_rerender_keeps_job_usable(monkeypatch, job):
    def failing(cmd, **kw):
        failed = Job.load(config.WORKSPACE, "j1")  # what the CLI does on error
        failed.status = JobStatus.failed
        failed.save(config.WORKSPACE)
        return SimpleNamespace(returncode=1, stdout="", stderr="RenderError: ffmpeg failed")

    monkeypatch.setattr(actions.subprocess, "run", failing)
    with pytest.raises(actions.ActionError, match="ffmpeg failed"):
        actions.edit_clip("j1", "c01", "h", 10, 30)
    assert Job.load(config.WORKSPACE, "j1").status == JobStatus.rendered


def test_cannot_edit_after_upload_started(monkeypatch, job):
    actions.approve("j1", "c01")
    [tt] = [p for p in actions.posts_for_clip("j1", "c01") if p.platform == Platform.tiktok]
    set_status(tt.id, PostStatus.uploaded_draft)
    calls = fake_run(monkeypatch)
    with pytest.raises(actions.ActionError, match="already been uploaded"):
        actions.edit_clip("j1", "c01", "h", 10, 30)
    assert calls == []


def test_can_edit_while_only_scheduled(monkeypatch, job):
    actions.approve("j1", "c01")
    fake_run(monkeypatch)
    actions.edit_clip("j1", "c01", "h", 10, 30)  # no error


def test_real_rerender_through_cli_and_ffmpeg(tmp_path):
    """End to end: edit_clip -> `clipper stage render --clip` -> ffmpeg -> new mp4."""
    source = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error"]
        + ["-f", "lavfi", "-i", "testsrc=duration=10:size=640x360:rate=30"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:duration=10"]
        + ["-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
        check=True,
    )
    j = Job(
        id="real",
        source_url="https://youtu.be/x",
        status=JobStatus.rendered,
        duration=10.0,
        source_file=source,
        clips=[Clip(id="c01", start=0, end=8, hook_text="first")],
    )
    j.save(config.WORKSPACE)

    actions.edit_clip("real", "c01", "Second hook", 2.0, 6.0)

    clip = Job.load(config.WORKSPACE, "real").clip("c01")
    assert clip.file and clip.file.exists()
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height"]
        + ["-of", "default=nw=1", str(clip.file)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "width=1080" in out and "height=1920" in out
    duration = float(next(x for x in out.splitlines() if x.startswith("duration=")).split("=")[1])
    assert abs(duration - 4.0) < 0.2
    assert "Second hook" in (clip.file.parent / "c01.ass").read_text(encoding="utf-8")


# ---------------------------------------------------------------- retry failed job


def test_resume_spawns_cli_and_appends_to_log(monkeypatch, job):
    job.status = JobStatus.failed
    job.save(config.WORKSPACE)
    log = Job.dir_for(config.WORKSPACE, "j1") / actions.LOG_NAME
    log.write_text("old output\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))

    actions.resume_job("j1")
    assert spawned[0][-2:] == ["resume", "j1"]
    assert log.read_text(encoding="utf-8").startswith("old output")


def test_resume_only_for_failed_jobs(job):
    with pytest.raises(actions.ActionError, match="failed"):
        actions.resume_job("j1")


def test_cli_resume_runs_every_stage_and_clears_error(monkeypatch, job):
    job.status, job.error = JobStatus.failed, "analyze: boom"
    job.save(config.WORKSPACE)
    ran = []

    def fake_stage(name, j, force, clip=None):
        ran.append((name, force, j.error))
        return j

    monkeypatch.setattr(cli, "_run_stage", fake_stage)
    result = CliRunner().invoke(cli.app, ["resume", "j1"])
    assert result.exit_code == 0, result.output
    assert ran == [(s, False, None) for s in cli.STAGES]


# ---------------------------------------------------------------- manual post fixes


@pytest.fixture
def ig_post(job):
    actions.approve("j1", "c01")
    [p] = [p for p in actions.posts_for_clip("j1", "c01") if p.platform == Platform.instagram]
    return p


def test_mark_posted_from_draft_saves_url(ig_post):
    set_status(ig_post.id, PostStatus.uploaded_draft)
    actions.mark_posted(ig_post.id, " https://tiktok.com/@me/1 ")
    post = next(p for p in actions.posts() if p.id == ig_post.id)
    assert post.status == PostStatus.published and post.url == "https://tiktok.com/@me/1"


def test_mark_posted_from_needs_check_without_url(ig_post):
    set_status(ig_post.id, PostStatus.needs_check)
    actions.mark_posted(ig_post.id, "")
    assert next(p for p in actions.posts() if p.id == ig_post.id).status == PostStatus.published


def test_mark_posted_rejects_scheduled_post(ig_post):
    with pytest.raises(actions.ActionError):
        actions.mark_posted(ig_post.id)


@pytest.mark.parametrize("status", [PostStatus.failed, PostStatus.needs_check])
def test_retry_reschedules_with_fresh_attempts(ig_post, status):
    conn = queue.connect()
    conn.execute(
        "UPDATE posts SET status = ?, attempts = 4, error = 'x' WHERE id = ?",
        (status.value, ig_post.id),
    )
    conn.close()
    post = actions.retry_post(ig_post.id)
    assert post.status == PostStatus.scheduled and post.attempts == 0 and post.error is None


def test_retry_respects_other_slots(tmp_path):
    conn = queue.connect(tmp_path / "q.db")
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    job = Job(id="jq", source_url="u")
    clips = [Clip(id=c, start=0, end=10, file=f, review="approved") for c in ("c01", "c02")]
    a = queue.enqueue(conn, job, clips[0], now=NOW, platforms=[Platform.instagram])[0]
    queue.enqueue(conn, job, clips[1], now=NOW, platforms=[Platform.instagram])  # 13:30
    conn.execute("UPDATE posts SET status = 'failed' WHERE id = ?", (a.id,))
    again = queue.requeue(conn, a.id, now=NOW)
    assert again.scheduled_at == NOW  # its old slot is free; c02 is 90 min away
    conn.execute("UPDATE posts SET status = 'failed' WHERE id = ?", (a.id,))
    later = queue.requeue(conn, a.id, now=NOW + timedelta(minutes=60))
    assert later.scheduled_at == NOW + timedelta(minutes=180)  # must clear c02 at 13:30
    conn.close()


def test_retry_refuses_live_posts(ig_post):
    set_status(ig_post.id, PostStatus.published)
    with pytest.raises(actions.ActionError):
        actions.retry_post(ig_post.id)


def test_give_up_only_from_needs_check(ig_post):
    with pytest.raises(actions.ActionError):
        actions.give_up_post(ig_post.id)
    set_status(ig_post.id, PostStatus.needs_check)
    actions.give_up_post(ig_post.id)
    assert next(p for p in actions.posts() if p.id == ig_post.id).status == PostStatus.failed


# ---------------------------------------------------------------- pages


def run_app(page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["page"] = page
    return at.run()


def test_jobs_page_has_edit_controls(job):
    at = run_app("Jobs")
    assert not at.exception
    assert at.text_input(key="hook-j1-c01").value == "old hook"
    assert at.number_input(key="start-j1-c01").value == 10.0
    assert at.button(key="rerender-j1-c01").disabled  # nothing edited yet


def test_failed_job_shows_retry(job):
    job.status, job.error = JobStatus.failed, "render: ffmpeg failed"
    job.save(config.WORKSPACE)
    at = run_app("Jobs")
    assert not at.exception
    assert "ffmpeg failed" in at.error[0].value
    assert at.button(key="resume-j1")


def test_queue_needs_check_buttons_and_sidebar_alert(ig_post):
    set_status(ig_post.id, PostStatus.needs_check)
    at = run_app("Queue")
    assert not at.exception
    assert any("need attention" in e.value for e in at.sidebar.error)
    at.button(key=f"live-{ig_post.id}").click().run()
    assert not at.exception
    assert next(p for p in actions.posts() if p.id == ig_post.id).status == PostStatus.published


def test_queue_draft_mark_as_posted(ig_post):
    conn = queue.connect()
    [tt] = queue.list_posts(conn, platform=Platform.tiktok)
    conn.close()
    set_status(tt.id, PostStatus.uploaded_draft)
    at = run_app("Queue")
    at.text_input(key=f"url-{tt.id}").input("https://tiktok.com/@me/9")
    at.button(key=f"posted-{tt.id}").click().run()
    assert not at.exception
    post = next(p for p in actions.posts() if p.id == tt.id)
    assert post.status == PostStatus.published and post.url == "https://tiktok.com/@me/9"
