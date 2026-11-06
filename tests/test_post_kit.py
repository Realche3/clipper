"""B5 post kit (manual posting) + the Queue and Sounds pages."""

from datetime import datetime, timedelta
from pathlib import Path

import pyperclip
import pytest
from streamlit.testing.v1 import AppTest

from clipper import config
from clipper.models import Clip, Job, JobStatus, Platform, PostStatus, ReviewStatus
from clipper.publish import queue
from clipper.sounds.store import Sound, SoundsFile, save_fetched
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
def side_effects(monkeypatch):
    """Record clipboard / file manager / browser calls instead of doing them."""
    seen = {"clipboard": [], "popen": [], "browser": []}
    monkeypatch.setattr(pyperclip, "copy", lambda text: seen["clipboard"].append(text))
    monkeypatch.setattr(actions.subprocess, "Popen", lambda cmd, **k: seen["popen"].append(cmd))
    monkeypatch.setattr(actions.webbrowser, "open_new_tab", lambda url: seen["browser"].append(url))
    return seen


@pytest.fixture
def job(tmp_path) -> Job:
    f = tmp_path / "c01.mp4"
    f.write_bytes(b"fake")
    j = Job(
        id="j1",
        source_url="https://youtu.be/x",
        credit="@creator",
        status=JobStatus.rendered,
        clips=[
            Clip(
                id="c01",
                start=0,
                end=30,
                title="Crash escape",
                caption="No way he made it",
                hashtags=["#gta6"],
                suggested_sound="Vogue — Madonna",
                file=f,
            )
        ],
    )
    j.save(config.WORKSPACE)
    return j


@pytest.fixture
def posts(job):
    actions.approve("j1", "c01")
    by_platform = {p.platform: p for p in actions.posts_for_clip("j1", "c01")}
    return by_platform[Platform.tiktok], by_platform[Platform.instagram]


# ---------------------------------------------------------------- queue states


def test_due_now_and_up_next_split_by_time(posts):
    tt, ig = posts
    assert {p.id for p in actions.due_now(tt.scheduled_at)} == {tt.id, ig.id}
    assert actions.due_now(tt.scheduled_at - timedelta(minutes=1)) == []
    assert {p.id for p in actions.up_next(tt.scheduled_at - timedelta(minutes=1))} == {
        tt.id,
        ig.id,
    }


def test_mark_posted_then_undo(posts):
    tt, _ = posts
    actions.mark_posted(tt.id, " https://www.tiktok.com/@me/video/1 ")
    post = queue.get_post(queue.connect(), tt.id)
    assert post.status == PostStatus.published and post.url.endswith("/video/1")
    assert tt.id not in {p.id for p in actions.due_now(datetime(2100, 1, 1))}

    with pytest.raises(actions.ActionError, match="already"):
        actions.mark_posted(tt.id)
    actions.undo_posted(tt.id)
    post = queue.get_post(queue.connect(), tt.id)
    assert post.status == PostStatus.scheduled and post.url is None
    with pytest.raises(actions.ActionError):
        actions.undo_posted(tt.id)


def test_posted_clip_keeps_its_slot_and_cannot_be_cancelled(posts):
    tt, _ = posts
    actions.mark_posted(tt.id)
    assert actions.cancel_post(tt.id) is False
    conn = queue.connect()
    assert tt.scheduled_at in queue.taken_slots(conn, Platform.tiktok)


# ---------------------------------------------------------------- post kit


def test_copy_text_is_platform_formatted(posts, side_effects):
    tt, ig = posts
    assert actions.copy_text(tt.id) == "No way he made it #gta6 🎥 @creator"
    actions.copy_text(ig.id)
    assert side_effects["clipboard"][-1] == "No way he made it\n\n#gta6\n🎥 @creator"


def test_open_upload_page_per_platform(posts, side_effects, monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_UPLOAD_URL", "https://tt/upload")
    monkeypatch.setattr(config, "INSTAGRAM_UPLOAD_URL", "https://ig/")
    tt, ig = posts
    actions.open_upload_page(tt.id)
    actions.open_upload_page(ig.id)
    assert side_effects["browser"] == ["https://tt/upload", "https://ig/"]


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("win32", lambda f: ["explorer", f"/select,{f}"]),
        ("darwin", lambda f: ["open", "-R", str(f)]),
        ("linux", lambda f: ["xdg-open", str(f.parent)]),
    ],
)
def test_reveal_file_per_os(posts, side_effects, monkeypatch, job, platform, expected):
    monkeypatch.setattr(actions.sys, "platform", platform)
    actions.reveal_file(posts[0].id)
    assert side_effects["popen"] == [expected(job.clips[0].file)]


def test_start_posting_does_all_three(posts, side_effects):
    actions.start_posting(posts[0].id)
    assert len(side_effects["clipboard"]) == 1
    assert len(side_effects["popen"]) == 1
    assert side_effects["browser"] == [config.TIKTOK_UPLOAD_URL]


def test_start_posting_with_missing_file_does_nothing(posts, side_effects, job):
    job.clips[0].file.unlink()
    with pytest.raises(actions.ActionError, match="missing"):
        actions.start_posting(posts[0].id)
    assert side_effects == {"clipboard": [], "popen": [], "browser": []}


def test_clipboard_failure_is_a_friendly_error(posts, monkeypatch):
    def broken(text):
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(pyperclip, "copy", broken)
    with pytest.raises(actions.ActionError, match="Copy the text from the box"):
        actions.copy_text(posts[0].id)


# ---------------------------------------------------------------- pages


def run_app(page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["page"] = page
    return at.run()


def test_queue_page_due_post_kit(posts, side_effects, monkeypatch):
    tt, ig = posts
    conn = queue.connect()  # approving books the next whole minute; make them due now
    past = (datetime.now() - timedelta(minutes=5)).isoformat()
    conn.execute("UPDATE posts SET scheduled_at = ?", (past,))
    conn.close()
    at = run_app("Queue")
    assert not at.exception
    assert any("2 post(s) due now" in s.value for s in at.sidebar.success)
    texts = [c.value for c in at.code]
    assert "No way he made it #gta6 🎥 @creator" in texts
    assert any("Vogue — Madonna" in m.value for m in at.markdown)

    at.button(key=f"start-{tt.id}").click().run()
    assert not at.exception
    assert side_effects["browser"] == [config.TIKTOK_UPLOAD_URL]

    at.text_input(key=f"url-{ig.id}").input("https://instagram.com/reel/1")
    at.button(key=f"posted-{ig.id}").click().run()
    assert not at.exception
    assert queue.get_post(queue.connect(), ig.id).url == "https://instagram.com/reel/1"
    assert at.button(key=f"undo-{ig.id}")  # now listed under Posted with an Undo


def test_queue_page_future_post_can_be_opened_early(job, monkeypatch):
    conn = queue.connect()
    job.clips[0].review = ReviewStatus.approved
    future = datetime.now() + timedelta(days=2)
    [p] = queue.enqueue(conn, job, job.clips[0], now=future, platforms=[Platform.tiktok])
    conn.close()
    at = run_app("Queue")
    assert not at.exception
    assert f"start-{p.id}" not in {b.key for b in at.button}  # not due: no post kit yet
    at.button(key=f"early-{p.id}").click().run()
    assert not at.exception
    assert at.button(key=f"start-{p.id}")


def test_sounds_page_lists_and_refreshes(monkeypatch):
    save_fetched(
        SoundsFile(fetched_at=datetime.now(), sounds=[Sound(title="Vogue", artist="Madonna")])
    )
    at = run_app("Sounds")
    assert not at.exception
    assert "Trending sounds (1)" in at.subheader[0].value
    assert len(at.dataframe) == 1

    monkeypatch.setattr(actions, "refresh", lambda: SoundsFile(sounds=[Sound(title="X")] * 3))
    at.button(key="refresh-sounds").click().run()
    assert not at.exception


def test_header_warns_when_sounds_never_fetched():
    at = run_app("New video")
    assert not at.exception
    assert any("No trending sounds yet" in w.value for w in at.warning)
