from datetime import datetime, timedelta

import pytest

from clipper import config
from clipper.models import Clip, Job, Platform, PostResult, PostStatus, ReviewStatus
from clipper.publish import queue, scheduler
from clipper.publish.clients import PublishError, UploadStateUnknown, make_clients
from clipper.publish.dryrun import DryRunClient
from clipper.publish.text import post_text

NOW = datetime(2026, 11, 19, 12, 0)


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(config, "DRY_RUN", True)
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


@pytest.fixture
def job(tmp_path) -> Job:
    """A rendered, approved job saved in the workspace, with a real (tiny) clip file."""
    clip_file = tmp_path / "c01.mp4"
    clip_file.write_bytes(b"fake mp4")
    j = Job(
        id="j1",
        source_url="https://youtu.be/x",
        credit="@creator",
        clips=[
            Clip(
                id="c01",
                start=0,
                end=30,
                caption="He did NOT see that coming",
                hashtags=["#gta6", "#gta6chase"],
                file=clip_file,
                review=ReviewStatus.approved,
            )
        ],
    )
    j.save(config.WORKSPACE)
    return j


class FakeClient:
    def __init__(self, platform, *outcomes):
        self.platform = platform
        self.outcomes = list(outcomes)
        self.calls = 0

    def publish(self, job, clip):
        self.calls += 1
        out = self.outcomes.pop(0) if self.outcomes else PostResult(status=PostStatus.published)
        if isinstance(out, Exception):
            raise out
        return out


def only(conn, platform=Platform.instagram):
    [post] = queue.list_posts(conn, platform=platform)
    return post


def enqueue_ig(conn, job, now=NOW):
    return queue.enqueue(conn, job, job.clips[0], now=now, platforms=[Platform.instagram])


# ---------------------------------------------------------------- happy path


def test_dry_run_posts_everything_due(conn, job):
    queue.enqueue(conn, job, job.clips[0], now=NOW)
    clients = make_clients([Platform.tiktok, Platform.instagram])
    assert all(isinstance(c, DryRunClient) for c in clients.values())

    assert scheduler.run_once(conn, clients, now=NOW) == 2
    assert only(conn, Platform.tiktok).status == PostStatus.uploaded_draft
    ig = only(conn, Platform.instagram)
    assert ig.status == PostStatus.published and ig.remote_id == "dryrun-j1-c01"
    assert scheduler.run_once(conn, clients, now=NOW) == 0  # nothing posted twice


def test_posts_not_due_are_left_alone(conn, job):
    enqueue_ig(conn, job)
    client = FakeClient(Platform.instagram)
    assert scheduler.run_once(conn, {Platform.instagram: client}, now=NOW - timedelta(1)) == 0
    assert client.calls == 0 and only(conn).status == PostStatus.scheduled


def test_only_platforms_with_a_client_are_claimed(conn, job):
    queue.enqueue(conn, job, job.clips[0], now=NOW)
    scheduler.run_once(conn, {Platform.instagram: FakeClient(Platform.instagram)}, now=NOW)
    assert only(conn, Platform.tiktok).status == PostStatus.scheduled


def test_success_saves_url(conn, job):
    enqueue_ig(conn, job)
    result = PostResult(status=PostStatus.published, remote_id="123", url="https://ig/p/123")
    scheduler.run_once(conn, {Platform.instagram: FakeClient(Platform.instagram, result)}, NOW)
    post = only(conn)
    assert (post.remote_id, post.url, post.attempts) == ("123", "https://ig/p/123", 1)


# ---------------------------------------------------------------- failures


def test_retryable_error_backs_off_then_fails(conn, job):
    enqueue_ig(conn, job)
    client = FakeClient(Platform.instagram, *[PublishError("503", retryable=True)] * 4)
    clients = {Platform.instagram: client}

    t = NOW
    for delay in (5, 15, 45):
        scheduler.run_once(conn, clients, now=t)
        post = only(conn)
        assert post.status == PostStatus.scheduled and post.error == "503"
        assert post.scheduled_at == t + timedelta(minutes=delay)
        t = post.scheduled_at

    scheduler.run_once(conn, clients, now=t)  # 4th try fails too
    post = only(conn)
    assert post.status == PostStatus.failed and post.attempts == 4 and client.calls == 4


def test_retry_then_success(conn, job):
    enqueue_ig(conn, job)
    client = FakeClient(Platform.instagram, PublishError("timeout", retryable=True))
    clients = {Platform.instagram: client}
    scheduler.run_once(conn, clients, now=NOW)
    scheduler.run_once(conn, clients, now=NOW + timedelta(minutes=5))
    assert only(conn).status == PostStatus.published


def test_permanent_error_fails_without_retry(conn, job):
    enqueue_ig(conn, job)
    client = FakeClient(Platform.instagram, PublishError("bad codec", retryable=False))
    scheduler.run_once(conn, {Platform.instagram: client}, now=NOW)
    post = only(conn)
    assert post.status == PostStatus.failed and post.error == "bad codec"


@pytest.mark.parametrize(
    "error", [UploadStateUnknown("dropped after publish"), RuntimeError("surprise")]
)
def test_unknown_outcome_needs_check_never_retries(conn, job, error):
    enqueue_ig(conn, job)
    client = FakeClient(Platform.instagram, error)
    clients = {Platform.instagram: client}
    scheduler.run_once(conn, clients, now=NOW)
    assert only(conn).status == PostStatus.needs_check
    scheduler.run_once(conn, clients, now=NOW + timedelta(hours=2))
    assert client.calls == 1


def test_client_returning_non_success_needs_check(conn, job):
    enqueue_ig(conn, job)
    bad = PostResult(status=PostStatus.failed, error="weird")
    scheduler.run_once(conn, {Platform.instagram: FakeClient(Platform.instagram, bad)}, NOW)
    assert only(conn).status == PostStatus.needs_check


def test_stuck_upload_becomes_needs_check(conn, job):
    enqueue_ig(conn, job)
    queue.claim_next(conn, NOW, [Platform.instagram])  # simulate a crash mid-upload
    client = FakeClient(Platform.instagram)
    scheduler.run_once(conn, {Platform.instagram: client}, now=NOW + timedelta(minutes=10))
    assert only(conn).status == PostStatus.uploading  # not stuck yet
    scheduler.run_once(conn, {Platform.instagram: client}, now=NOW + timedelta(minutes=16))
    assert only(conn).status == PostStatus.needs_check and client.calls == 0


def test_missing_rendered_file_fails_before_upload(conn, job):
    enqueue_ig(conn, job)
    job.clips[0].file.unlink()
    client = FakeClient(Platform.instagram)
    scheduler.run_once(conn, {Platform.instagram: client}, now=NOW)
    post = only(conn)
    assert post.status == PostStatus.failed and "missing" in post.error and client.calls == 0


def test_missing_manifest_fails_before_upload(conn, job):
    enqueue_ig(conn, job)
    (Job.dir_for(config.WORKSPACE, "j1") / "manifest.json").unlink()
    client = FakeClient(Platform.instagram)
    scheduler.run_once(conn, {Platform.instagram: client}, now=NOW)
    assert only(conn).status == PostStatus.failed and client.calls == 0


def test_retry_outside_window_moves_to_next_morning(conn, job):
    late = datetime(2026, 11, 19, 23, 28)
    enqueue_ig(conn, job, now=late)
    client = FakeClient(Platform.instagram, PublishError("503", retryable=True))
    scheduler.run_once(conn, {Platform.instagram: client}, now=late)
    assert only(conn).scheduled_at == datetime(2026, 11, 20, 10, 0)


# ---------------------------------------------------------------- clients, text, loop


def test_live_mode_without_real_clients_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    with pytest.raises(NotImplementedError, match="DRY_RUN"):
        make_clients([Platform.tiktok])


def test_post_text_per_platform(job):
    clip = job.clips[0]
    assert post_text(job, clip, Platform.instagram) == (
        "He did NOT see that coming\n\n#gta6 #gta6chase\n🎥 @creator"
    )
    assert post_text(job, clip, Platform.tiktok) == (
        "He did NOT see that coming #gta6 #gta6chase 🎥 @creator"
    )
    job.credit = None
    assert post_text(job, clip, Platform.tiktok) == "He did NOT see that coming #gta6 #gta6chase"


def test_run_forever_ticks_until_stopped(conn, job):
    ticks = []

    def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever(conn, {}, tick_seconds=60, sleep=fake_sleep)
    assert ticks == [60, 60]
