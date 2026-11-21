"""YouTube Shorts in the manual post kit: title + description, upload page, queue slots."""

from datetime import datetime, timedelta
from pathlib import Path

import pyperclip
import pytest
from streamlit.testing.v1 import AppTest

from clipper import config
from clipper.captions.generate import clean_title
from clipper.models import Clip, Job, JobStatus, Platform
from clipper.publish import queue
from clipper.publish.text import post_text, post_title
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
    monkeypatch.setattr(config, "ENABLED_PLATFORMS", ["tiktok", "instagram", "youtube"])
    monkeypatch.setattr(config, "POSTING_WINDOW", "00:00-23:59")


@pytest.fixture
def side_effects(monkeypatch):
    seen = {"clipboard": [], "popen": [], "browser": []}
    monkeypatch.setattr(pyperclip, "copy", lambda text: seen["clipboard"].append(text))
    monkeypatch.setattr(actions.subprocess, "Popen", lambda cmd, **k: seen["popen"].append(cmd))
    monkeypatch.setattr(actions.webbrowser, "open_new_tab", lambda url: seen["browser"].append(url))
    return seen


def make_job(tmp_path, **clip_fields) -> Job:
    f = tmp_path / "c01.mp4"
    f.write_bytes(b"fake")
    fields = {
        "title": "GTA 6 police chase ends in the ocean 🌊",
        "caption": "No way he made it",
        "hashtags": ["#gta6"],
        **clip_fields,
    }
    job = Job(
        id="j1",
        source_url="https://youtu.be/x",
        credit="@creator",
        status=JobStatus.rendered,
        clips=[Clip(id="c01", start=0, end=30, file=f, **fields)],
    )
    job.save(config.WORKSPACE)
    return job


@pytest.fixture
def yt_post(tmp_path):
    make_job(tmp_path)
    actions.approve("j1", "c01")
    [yt] = [p for p in actions.posts_for_clip("j1", "c01") if p.platform == Platform.youtube]
    return yt


# ---------------------------------------------------------------- text


def test_youtube_description_adds_shorts_tag_and_credit(tmp_path):
    job = make_job(tmp_path)
    assert post_text(job, job.clips[0], Platform.youtube) == (
        "No way he made it\n\n#gta6 #shorts\n🎥 @creator"
    )


def test_youtube_description_does_not_repeat_shorts_tag(tmp_path):
    job = make_job(tmp_path, hashtags=["#gta6", "#Shorts"])
    assert post_text(job, job.clips[0], Platform.youtube).count("#") == 2


def test_post_title_uses_clip_title_and_youtube_rules(tmp_path):
    job = make_job(tmp_path)
    assert post_title(job, job.clips[0]) == "GTA 6 police chase ends in the ocean 🌊"
    job.clips[0].title = "<b>Huge</b>   " + "word " * 40
    title = post_title(job, job.clips[0])
    assert "<" not in title and ">" not in title and "  " not in title
    assert len(title) <= 100 and title.endswith("…")


def test_post_title_falls_back_to_hook_then_video_title(tmp_path):
    job = make_job(tmp_path, title=None, hook_text="He jumped the bridge")
    assert post_title(job, job.clips[0]) == "He jumped the bridge"
    job.clips[0].hook_text = None
    job.title = "GTA 6 launch stream"
    assert post_title(job, job.clips[0]) == "GTA 6 launch stream"


def test_clean_title_strips_hashtags_and_brackets():
    assert clean_title("  Big <crash> #gta6 moment ") == "Big crash moment"
    assert len(clean_title("x" * 150)) <= 100


# ---------------------------------------------------------------- queue + post kit


def test_approve_queues_youtube_with_its_own_cap(yt_post, monkeypatch):
    assert yt_post.platform == Platform.youtube
    monkeypatch.setattr(config, "DAILY_CAP_YOUTUBE", 3)
    assert queue.daily_cap(Platform.youtube) == 3


def test_start_posting_youtube_copies_title_and_opens_studio(yt_post, side_effects):
    actions.start_posting(yt_post.id)
    assert side_effects["clipboard"] == ["GTA 6 police chase ends in the ocean 🌊"]
    assert side_effects["browser"] == [config.YOUTUBE_UPLOAD_URL]
    assert len(side_effects["popen"]) == 1
    actions.copy_text(yt_post.id)
    assert side_effects["clipboard"][-1].startswith("No way he made it\n\n#gta6 #shorts")


def test_save_copy_edits_the_title(tmp_path):
    make_job(tmp_path)
    actions.save_copy("j1", "c01", "new caption", "#gta6", "  New   title ")
    _, clip = actions.load_clip("j1", "c01")
    assert clip.title == "New title"
    actions.save_copy("j1", "c01", "new caption", "#gta6")  # no title given: kept
    assert actions.load_clip("j1", "c01")[1].title == "New title"


# ---------------------------------------------------------------- pages


def run_app(page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["page"] = page
    return at.run()


def test_jobs_page_shows_editable_youtube_title(tmp_path):
    make_job(tmp_path)
    at = run_app("Jobs")
    assert not at.exception
    box = at.text_input(key="title-j1-c01")
    assert box.value == "GTA 6 police chase ends in the ocean 🌊"
    box.input("Better title").run()
    at.button(key="save-j1-c01").click().run()
    assert not at.exception
    assert actions.load_clip("j1", "c01")[1].title == "Better title"


def test_queue_page_youtube_post_kit(yt_post, side_effects):
    conn = queue.connect()
    past = (datetime.now() - timedelta(minutes=5)).isoformat()
    conn.execute("UPDATE posts SET scheduled_at = ?", (past,))
    conn.close()
    at = run_app("Queue")
    assert not at.exception
    texts = [c.value for c in at.code]
    assert "GTA 6 police chase ends in the ocean 🌊" in texts
    assert "No way he made it\n\n#gta6 #shorts\n🎥 @creator" in texts
    at.button(key=f"copy-title-{yt_post.id}").click().run()
    assert not at.exception
    assert side_effects["clipboard"] == ["GTA 6 police chase ends in the ocean 🌊"]
