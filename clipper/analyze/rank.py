"""Claude picks the final clips from the signal candidates. Spec: docs/ARCHITECTURE.md §5.2.4.

Claude scores every candidate; code enforces validity and the clip count, and falls back to the
signal ranking for anything Claude doesn't deliver (including an LLMError).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from clipper import llm
from clipper.analyze.candidates import Candidate
from clipper.models import Clip, Job, Transcript

log = logging.getLogger(__name__)

MAX_OVERLAP = 0.5  # of the shorter clip; from AutoClip's _dedupe (MIT)


class RankedClip(BaseModel):
    index: int
    start: float
    end: float
    score: float
    reason: str


class Ranking(BaseModel):
    clips: list[RankedClip]


SYSTEM = """\
You pick highlight moments from a long video to post as vertical short clips on TikTok \
and Instagram Reels. Viewers scroll fast: a good clip has a clear moment (a reaction, a \
fail, a surprise, a funny line, a big play) and starts right on the action.

Work out what the video is about from the poster's note (most trusted), then the title \
and description. You get candidate windows found from audio loudness, scene cuts and \
speech, each with its stats and transcript.

Score every candidate from 0 to 1 for how well it would do as a short clip. For each one \
return its index, a start and end inside its window (trim dead air; start on the action), \
the score, and a one-sentence reason saying what happens in it. Base the reason only on \
what you are given; do not invent events."""


def build_prompt(
    job: Job, candidates: list[Candidate], transcript: Transcript, clip_min: float, clip_max: float
) -> str:
    lines = [
        f"Poster's note: {job.context or '(none)'}",
        f"Source video title: {job.title or 'unknown'}",
        f"Source description:\n{job.description or '(none)'}",
        "",
        f"Each clip must be {clip_min:.0f}-{clip_max:.0f} seconds long.",
        "Candidates:",
    ]
    for i, c in enumerate(candidates):
        s = c.stats
        lines += [
            f"- index: {i}",
            f"  window: {c.start:.1f}-{c.end:.1f}s",
            f"  loudness z at peak: {s['loud_z']}, scene cuts: {s['cuts']}, "
            f"words/s: {s['words_per_s']}",
            f"  transcript: {transcript.text_between(c.start, c.end) or '(no speech)'}",
        ]
    return "\n".join(lines)


def rank(
    job: Job,
    candidates: list[Candidate],
    transcript: Transcript,
    n: int,
    clip_min: float,
    clip_max: float,
) -> list[Clip]:
    """The best n clips: Claude's valid picks first, then signal candidates to fill the gap."""
    try:
        answer = llm.ask_json(
            SYSTEM, build_prompt(job, candidates, transcript, clip_min, clip_max), Ranking
        ).clips
    except llm.LLMError as e:
        log.warning("ranker fell back to signal scores: %s", e)
        answer = []

    picks = [p for p in (_valid(r, candidates, clip_min, clip_max) for r in answer) if p]
    picks.sort(key=lambda p: -p[2])
    fills = [
        (c.start, c.end, 0.0, f"Picked by audio/video signals ({_describe(c)}).")
        for c in candidates
    ]
    chosen: list[tuple[float, float, float, str]] = []
    for p in picks + fills:
        if len(chosen) == n:
            break
        if all(_overlap(p, q) <= MAX_OVERLAP for q in chosen):
            chosen.append(p)
    log.info("ranker: %d of %d Claude picks valid; %d clips", len(picks), len(answer), len(chosen))

    chosen.sort(key=lambda p: p[0])
    return [
        Clip(id=f"c{i:02d}", start=s, end=e, score=score, reason=reason)
        for i, (s, e, score, reason) in enumerate(chosen, start=1)
    ]


def _valid(
    r: RankedClip, candidates: list[Candidate], clip_min: float, clip_max: float
) -> tuple[float, float, float, str] | None:
    """Clamp a pick into its candidate window; None if the index or the length is bad."""
    if not 0 <= r.index < len(candidates):
        return None
    c = candidates[r.index]
    start, end = max(r.start, c.start), min(r.end, c.end)
    if not clip_min <= end - start <= clip_max:
        return None
    return round(start, 2), round(end, 2), min(max(r.score, 0.0), 1.0), r.reason.strip()


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Overlap as a fraction of the shorter clip."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    return max(inter, 0.0) / min(a[1] - a[0], b[1] - b[0])


def _describe(c: Candidate) -> str:
    s = c.stats
    return f"loudness z {s['loud_z']}, {s['cuts']} cuts, {s['words_per_s']} words/s"
