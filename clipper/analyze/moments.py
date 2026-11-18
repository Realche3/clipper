"""Claude proposes clip moments from the whole transcript. Spec: docs/ARCHITECTURE.md §5.2.3.

Signal peaks miss moments carried by what is said at normal volume (a story, a joke, a hot
take). These windows join the signal candidates and the ranker judges them all together.
Any Claude problem -> no extra windows; the signal candidates still stand.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from clipper import config, llm
from clipper.analyze.candidates import fit_window
from clipper.analyze.rank import timed_lines
from clipper.models import Job, Transcript

log = logging.getLogger(__name__)


class Moment(BaseModel):
    start: float
    end: float


class Moments(BaseModel):
    moments: list[Moment]


SYSTEM = """\
You read the transcript of a long video and find moments that would work as vertical short \
clips on TikTok and Instagram Reels. Each transcript line starts with its time in the \
video, in seconds.

Work out what the video is about from the poster's note (most trusted), then the title \
and description.

Look for moments carried by what is said: a short story with an ending, a joke and its \
punchline, a strong or surprising opinion, a reaction with enough lead-up to understand \
it. A good moment hooks in its first 3 seconds, makes sense to someone who has not seen \
the rest of the video, and ends on its payoff.

Return each moment's start and end in seconds, taken from the transcript times, starting \
on the line where it begins and ending at the start time of the line after the one where \
it lands. Return the best \
moments only, best first, and never more than you are asked for. Return an empty list if \
nothing in the transcript stands on its own."""


def build_prompt(job: Job, transcript: Transcript, k: int, clip_min: float, clip_max: float) -> str:
    words = [w for s in transcript.segments for w in s.words]
    return "\n".join(
        [
            f"Poster's note: {job.context or '(none)'}",
            f"Source video title: {job.title or 'unknown'}",
            f"Source description:\n{job.description or '(none)'}",
            "",
            f"Find up to {k} moments, each {clip_min:.0f}-{clip_max:.0f} seconds long.",
            "Transcript:",
            *timed_lines(words),
        ]
    )


def propose(
    job: Job, transcript: Transcript, duration: float, k: int, clip_min: float, clip_max: float
) -> list[tuple[float, float]]:
    """Up to k (start, end) windows, fitted to the clip length limits and the source."""
    try:
        answer = llm.ask_json(
            SYSTEM,
            build_prompt(job, transcript, k, clip_min, clip_max),
            Moments,
            model=config.CLAUDE_MODEL_MOMENTS,
            task="moments",
        ).moments
    except llm.LLMError as e:
        log.warning("no transcript moments, signal candidates only: %s", e)
        return []
    windows = [
        fit_window(m.start, m.end, duration, clip_min, clip_max)
        for m in answer
        if 0 <= m.start < m.end and m.start < duration
    ]
    log.info("transcript moments: %d of %d usable", len(windows), len(answer))
    return windows[:k]
