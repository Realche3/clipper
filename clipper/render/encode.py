"""Render stage: cut each clip and encode it as a 1080x1920 vertical video.

Spec: docs/ARCHITECTURE.md §5.4. Subtitles and the hook are burned in from <clip_id>.ass.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from clipper import config
from clipper.models import Clip, Job, JobStatus, Transcript, Word
from clipper.render.subtitles import build_ass, group_words

log = logging.getLogger(__name__)

# The source, scaled to fill 9:16 and blurred, with the full-width source centred on top.
BLUR_LAYOUT = (
    "[0:v]split[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];"
    "[b]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
)
MIN_BYTES = 1024


class RenderError(Exception):
    pass


def ffmpeg_args(
    source: Path, start: float, end: float, out_name: str, ass_name: str | None = None
) -> list[str]:
    # A relative .ass name: ffmpeg's subtitles= filter breaks on absolute Windows paths.
    graph = BLUR_LAYOUT + (f",subtitles={ass_name}" if ass_name else "") + "[v]"
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a:0",
        "-af", "loudnorm=I=-14:TP=-1.5",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart",
        out_name,
    ]  # fmt: skip


def render_clip(source: Path, clip: Clip, clips_dir: Path, ass_name: str | None = None) -> Path:
    """Encode one clip into clips_dir/<id>.mp4. ffmpeg runs with cwd=clips_dir so the
    subtitles filter can use a relative .ass name, which Windows needs."""
    out = clips_dir / f"{clip.id}.mp4"
    tmp_name = f"{clip.id}.tmp.mp4"
    try:
        subprocess.run(
            ffmpeg_args(source, clip.start, clip.end, tmp_name, ass_name),
            cwd=clips_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RenderError(f"ffmpeg failed on clip {clip.id}: {e.stderr[-500:]}") from e
    tmp = clips_dir / tmp_name
    if not tmp.exists() or tmp.stat().st_size < MIN_BYTES:
        raise RenderError(f"ffmpeg wrote no usable output for clip {clip.id}")
    os.replace(tmp, out)
    return out


def run(job: Job, force: bool = False, clip_id: str | None = None) -> Job:
    if job.source_file is None or not Path(job.source_file).exists():
        raise RuntimeError("source.mp4 is missing; run the ingest stage first")
    if clip_id and clip_id not in {c.id for c in job.clips}:
        raise ValueError(f"job {job.id} has no clip {clip_id!r}")
    clips_dir = Job.dir_for(config.WORKSPACE, job.id).resolve() / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    words = _words(job)

    for clip in [job.clip(clip_id)] if clip_id else job.clips:
        out = clips_dir / f"{clip.id}.mp4"
        if out.exists() and not force:
            log.info("clip %s already rendered; skipping (use --force to redo)", clip.id)
        else:
            log.info("rendering clip %s (%.1f-%.1fs)", clip.id, clip.start, clip.end)
            ass = build_ass(group_words(words, clip.start, clip.end), clip.hook_text)
            ass_name = f"{clip.id}.ass" if ass else None
            if ass_name:
                (clips_dir / ass_name).write_text(ass, encoding="utf-8")
            render_clip(Path(job.source_file), clip, clips_dir, ass_name)
        clip.file = out
    job.status = JobStatus.rendered
    return job


def _words(job: Job) -> list[Word]:
    path = Path(job.transcript_file) if job.transcript_file else None
    if path and not path.is_absolute():
        path = config.ROOT / path
    if path is None or not path.exists():
        log.warning("no transcript for job %s; rendering without subtitles", job.id)
        return []
    transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    return [w for s in transcript.segments for w in s.words]
