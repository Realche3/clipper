"""Ingest stage: download a YouTube video with yt-dlp, then extract audio.wav.

Spec: docs/ARCHITECTURE.md §5.1. Sources are always YouTube URLs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from clipper import config
from clipper.ingest.media import extract_audio, probe_duration
from clipper.models import Job, JobStatus

log = logging.getLogger(__name__)

# <=1080p mp4. H.264 first: plain ext=mp4 can be AV1, which decodes slowly on laptops.
FORMAT = (
    "bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
    "/bv*[height<=1080][ext=mp4]+ba[ext=m4a]"
    "/b[height<=1080]"
)


class IngestError(Exception):
    """The source can't be used (too long, blocked, not a URL)."""


def run(job: Job, force: bool = False) -> Job:
    if not job.source_url.startswith(("http://", "https://")):
        raise IngestError(f"source must be a YouTube URL, got {job.source_url!r}")

    job_dir = Job.dir_for(config.WORKSPACE, job.id).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "source.mp4"
    audio = job_dir / "audio.wav"

    if force or not source.exists() or job.title is None:
        info = fetch(job.source_url, job_dir, overwrite=force)
        for key, value in metadata(info).items():
            setattr(job, key, value)
    else:
        log.info("source.mp4 already downloaded; skipping (use --force to redo)")
    if not source.exists():
        raise IngestError(f"yt-dlp finished but {source.name} is missing")

    job.duration = probe_duration(source)  # trust the file over yt-dlp's metadata
    _check_length(job.duration)
    job.source_file = source

    if force or not audio.exists():
        extract_audio(source, audio)
    job.audio_file = audio

    job.status = JobStatus.ingested
    return job


def ydl_options(job_dir: Path, *, cookies: bool = True, overwrite: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": FORMAT,
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        # a single-file fallback format may be webm; always end with source.mp4
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "noplaylist": True,  # watch?v=X&list=... means the one video
        "overwrites": overwrite,
        "file_access_retries": 8,  # Windows: antivirus/indexer briefly locks the file
        "quiet": True,
        "no_warnings": True,
    }
    if cookies and config.YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (config.YTDLP_COOKIES_FROM_BROWSER,)
    return opts


def fetch(url: str, job_dir: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Read metadata, reject over-long sources before downloading, then download."""
    try:
        return _fetch(url, job_dir, cookies=True, overwrite=overwrite)
    except (DownloadError, CookieLoadError) as e:
        if not (config.YTDLP_COOKIES_FROM_BROWSER and "cookie" in str(e).lower()):
            raise _explain(e) from e
        # Chrome on Windows locks its cookie database while it is open.
        log.warning(
            "couldn't read %s cookies (%s); retrying without them",
            config.YTDLP_COOKIES_FROM_BROWSER,
            e,
        )
        try:
            return _fetch(url, job_dir, cookies=False, overwrite=overwrite)
        except DownloadError as e2:
            raise _explain(e2) from e2


def _fetch(url: str, job_dir: Path, *, cookies: bool, overwrite: bool) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_options(job_dir, cookies=cookies, overwrite=overwrite)) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("duration"):
            _check_length(float(info["duration"]))
        return ydl.process_ie_result(info, download=True)


def metadata(info: dict[str, Any]) -> dict[str, str | None]:
    channel = info.get("channel") or info.get("uploader")
    handle = (info.get("uploader_id") or "").lstrip("@")
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "channel": channel,
        "credit": f"@{handle}" if handle else channel,
    }


def _check_length(seconds: float) -> None:
    if seconds > config.MAX_SOURCE_MINUTES * 60:
        raise IngestError(
            f"source is {seconds / 60:.0f} min, over MAX_SOURCE_MINUTES={config.MAX_SOURCE_MINUTES}"
        )


def _explain(e: Exception) -> Exception:
    text = str(e).lower()
    if "not a bot" in text or "sign in to confirm" in text:
        return IngestError(
            "YouTube asked for a bot check. Set YTDLP_COOKIES_FROM_BROWSER (e.g. chrome, "
            "with that browser closed) and update yt-dlp: uv lock --upgrade-package yt-dlp"
        )
    return e
