"""Analyze stage: transcript -> signals + candidates -> Claude ranker -> clips.

Spec: docs/ARCHITECTURE.md §5.2. Status becomes `analyzed`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from clipper import config
from clipper.analyze import signals
from clipper.analyze.candidates import Candidate, find_candidates
from clipper.analyze.rank import rank
from clipper.analyze.transcribe import transcribe
from clipper.models import Job, JobStatus, Transcript

log = logging.getLogger(__name__)


def run(job: Job, force: bool = False) -> Job:
    for f in (job.audio_file, job.source_file):
        if f is None or not Path(f).exists():
            raise RuntimeError("source.mp4 or audio.wav is missing; run the ingest stage first")

    job_dir = Job.dir_for(config.WORKSPACE, job.id).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "transcript.json"
    if force or not path.exists():
        transcript = transcribe(Path(job.audio_file), config.WHISPER_MODEL)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
    else:
        log.info("transcript.json exists; skipping transcription (use --force to redo)")
    job.transcript_file = path

    if job.clips and not force:
        log.info("clips already picked; skipping ranking (use --force to redo)")
    else:
        transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
        candidates = _candidates(job, transcript)
        for c in candidates:
            log.info("candidate %.1f-%.1fs score=%.2f %s", c.start, c.end, c.score, c.stats)
        job.clips = rank(
            job,
            candidates,
            transcript,
            n=config.CLIPS_PER_VIDEO,
            clip_min=config.CLIP_MIN_SECONDS,
            clip_max=config.CLIP_MAX_SECONDS,
        )
    job.status = JobStatus.analyzed
    return job


def _candidates(job: Job, transcript: Transcript) -> list[Candidate]:
    samples, sr = signals.read_wav(Path(job.audio_file))
    loud = signals.loudness_per_second(samples, sr)
    n = len(loud)
    cuts = signals.detect_cuts(Path(job.source_file))
    score = signals.combine(
        loud, signals.cut_density(cuts, n), signals.speech_per_second(transcript, n)
    )
    return find_candidates(
        score,
        signals.zscore(loud),
        cuts,
        transcript,
        duration=job.duration or n,
        k=config.CANDIDATES,
        clip_min=config.CLIP_MIN_SECONDS,
        clip_max=config.CLIP_MAX_SECONDS,
    )
