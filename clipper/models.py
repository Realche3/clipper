"""Shared contract between pipeline stages. See CLAUDE.md and docs/ARCHITECTURE.md.

Rule: only ADD optional fields. Renames/removals need both devs to agree.
"""

from __future__ import annotations

import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- transcript
# Written by analyze (transcript.json), read by render (subtitles) and captions.


class Word(BaseModel):
    start: float
    end: float
    text: str


class Segment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str | None = None
    segments: list[Segment] = Field(default_factory=list)

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for s in self.segments for w in s.words if w.start >= start and w.end <= end]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text.strip() for w in self.words_between(start, end))


# ---------------------------------------------------------------- job / clips
# Pipeline order: ingest -> analyze -> captions -> render -> (review) -> publish queue


class JobStatus(StrEnum):
    created = "created"
    ingested = "ingested"
    analyzed = "analyzed"
    captioned = "captioned"
    rendered = "rendered"  # = ready for review
    failed = "failed"


class ReviewStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Clip(BaseModel):
    id: str  # "c01"
    # analyze
    start: float  # seconds in source
    end: float
    score: float = 0.0  # 0..1
    reason: str = ""
    # captions
    hook_text: str | None = None  # on-screen first 3s, <= 6 words
    title: str | None = None
    caption: str | None = None  # no hashtags
    hashtags: list[str] = Field(default_factory=list)
    vibe: str | None = None  # hype | funny | chill | dramatic
    suggested_sound: str | None = None
    # render
    file: Path | None = None
    # review (dashboard)
    review: ReviewStatus = ReviewStatus.pending

    @property
    def duration(self) -> float:
        return self.end - self.start


class HeatPoint(BaseModel):
    """One span of YouTube's "most replayed" graph. value is 0..1, 1 = the most replayed."""

    start: float
    end: float
    value: float


class Job(BaseModel):
    id: str
    source_url: str  # YouTube URL or local file path
    # user's note at submit time ("GTA 6 launch, funny police fails"); steers captions/tags
    context: str | None = None
    status: JobStatus = JobStatus.created
    created_at: datetime = Field(default_factory=datetime.now)
    # ingest
    title: str | None = None
    description: str | None = None  # source video description (YouTube)
    channel: str | None = None
    credit: str | None = None  # e.g. "@channel"; None for our own gameplay
    duration: float | None = None
    source_file: Path | None = None
    audio_file: Path | None = None  # 16kHz mono wav
    heatmap: list[HeatPoint] = Field(default_factory=list)  # empty if YouTube shows no graph
    # analyze
    transcript_file: Path | None = None
    clips: list[Clip] = Field(default_factory=list)
    error: str | None = None

    def clip(self, clip_id: str) -> Clip:
        return next(c for c in self.clips if c.id == clip_id)

    # --- persistence ---
    @staticmethod
    def dir_for(workspace: Path, job_id: str) -> Path:
        return workspace / "jobs" / job_id

    @classmethod
    def load(cls, workspace: Path, job_id: str) -> Job:
        path = cls.dir_for(workspace, job_id) / "manifest.json"
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, workspace: Path) -> Path:
        """Atomic write: never leaves a half-written manifest."""
        d = self.dir_for(workspace, self.id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "manifest.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path


# ---------------------------------------------------------------- post queue
# Rows in workspace/clipper.db, owned by clipper/publish. UNIQUE(job_id, clip_id, platform).


class Platform(StrEnum):
    tiktok = "tiktok"
    instagram = "instagram"


class PostStatus(StrEnum):
    scheduled = "scheduled"
    uploading = "uploading"  # claimed by scheduler
    uploaded_draft = "uploaded_draft"  # TikTok inbox: finish in app (sound + caption)
    published = "published"
    failed = "failed"  # retries exhausted
    needs_check = "needs_check"  # stuck in uploading; human must verify, never auto-retry


class Post(BaseModel):
    id: int | None = None
    job_id: str
    clip_id: str
    platform: Platform
    status: PostStatus = PostStatus.scheduled
    scheduled_at: datetime
    attempts: int = 0
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=datetime.now)


class PostResult(BaseModel):
    status: PostStatus
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None
