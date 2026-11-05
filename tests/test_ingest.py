import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from clipper import config
from clipper.ingest import download
from clipper.models import Job, JobStatus

URL = "https://www.youtube.com/watch?v=abc123"
INFO = {
    "title": "GTA 6 - Insane Police Chase",
    "description": "Trying to escape 5 stars.",
    "channel": "Example Channel",
    "uploader_id": "@example_channel",
    "duration": 5,
}


@pytest.fixture(scope="module")
def sample_mp4(tmp_path_factory) -> Path:
    """5s 320x240 test pattern with a tone, made locally (no network)."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error"]
        + ["-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=30"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:duration=5"]
        + ["-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def fake_ydl(monkeypatch, tmp_path, sample_mp4):
    """Replace yt_dlp.YoutubeDL; 'downloading' copies the sample to outtmpl."""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    calls: dict = {"opts": [], "downloads": 0, "info": dict(INFO), "fail_with": None}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            calls["opts"].append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            if calls["fail_with"] and "cookiesfrombrowser" in self.opts:
                raise DownloadError(calls["fail_with"])
            return dict(calls["info"])

        def process_ie_result(self, info, download):
            calls["downloads"] += 1
            shutil.copy(sample_mp4, self.opts["outtmpl"] % {"ext": "mp4"})
            return info

    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", FakeYDL)
    return calls


def test_ingest_downloads_and_extracts_audio(fake_ydl):
    job = download.run(Job(id="j1", source_url=URL))

    assert job.status == JobStatus.ingested
    assert job.title == INFO["title"]
    assert job.description == INFO["description"]
    assert job.channel == "Example Channel"
    assert job.credit == "@example_channel"
    assert job.duration == pytest.approx(5, abs=0.2)
    assert job.source_file.name == "source.mp4" and job.source_file.is_absolute()
    with wave.open(str(job.audio_file)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)


def test_rerun_skips_download(fake_ydl):
    job = download.run(Job(id="j1", source_url=URL))
    download.run(job)
    assert fake_ydl["downloads"] == 1
    download.run(job, force=True)
    assert fake_ydl["downloads"] == 2


def test_too_long_is_rejected_before_download(fake_ydl, monkeypatch):
    monkeypatch.setattr(config, "MAX_SOURCE_MINUTES", 1)
    fake_ydl["info"]["duration"] = 3600
    with pytest.raises(download.IngestError, match="MAX_SOURCE_MINUTES"):
        download.run(Job(id="j1", source_url=URL))
    assert fake_ydl["downloads"] == 0


def test_local_path_is_rejected(fake_ydl):
    with pytest.raises(download.IngestError, match="YouTube URL"):
        download.run(Job(id="j1", source_url="C:/videos/clip.mp4"))


def test_options(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FROM_BROWSER", "chrome")
    opts = download.ydl_options(tmp_path)
    assert "vcodec^=avc1" in opts["format"] and "height<=1080" in opts["format"]
    assert opts["noplaylist"] and opts["merge_output_format"] == "mp4"
    assert opts["cookiesfrombrowser"] == ("chrome",)
    assert "cookiesfrombrowser" not in download.ydl_options(tmp_path, cookies=False)


def test_locked_cookie_db_retries_without_cookies(fake_ydl, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FROM_BROWSER", "chrome")
    fake_ydl["fail_with"] = "Could not copy Chrome cookie database"
    job = download.run(Job(id="j1", source_url=URL))
    assert job.status == JobStatus.ingested
    assert "cookiesfrombrowser" not in fake_ydl["opts"][-1]


def test_bot_check_gives_actionable_error(fake_ydl, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FROM_BROWSER", "")

    def blocked(self, url, download):
        raise DownloadError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(download.yt_dlp.YoutubeDL, "extract_info", blocked)
    with pytest.raises(download.IngestError, match="YTDLP_COOKIES_FROM_BROWSER"):
        download.run(Job(id="j1", source_url=URL))


def test_credit_falls_back_to_channel():
    assert download.metadata({"channel": "Some Channel"})["credit"] == "Some Channel"
