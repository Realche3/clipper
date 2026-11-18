"""Claude picks the final clips from the signal candidates. Spec: docs/ARCHITECTURE.md §5.2.4.

Claude scores every candidate; code enforces validity and the clip count, and falls back to the
signal ranking for anything Claude doesn't deliver (including an LLMError).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from clipper import config, llm
from clipper.analyze.candidates import PAUSE_GAP, Candidate
from clipper.models import Clip, Job, Transcript, Word

log = logging.getLogger(__name__)

# Of the shorter clip (the idea is from AutoClip's _dedupe, MIT). Low, because two posts that
# share several seconds of footage read as a repost.
MAX_OVERLAP = 0.2
LINE_MAX_WORDS = 12  # transcript lines in the prompt break at pauses, or after this many words


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
and description. You get candidate windows, each found either from signals (audio \
loudness, scene cuts, speech) or from the transcript (something said that could stand as a \
clip). Each has its stats and a transcript whose lines start with their time in the source \
video. "most replayed", when it is there, is how much YouTube viewers rewatched the \
window's best part, from 0 to 1: treat a high value as real evidence that the moment works.

Score every candidate from 0 to 1 for how well it would do as a short clip. Judge three \
things and let the weakest one hold the score down:
- Hook: do the first 3 seconds give a viewer a reason to stay?
- Standalone: does it make sense to someone who has not seen the rest of the video?
- Payoff: does the moment land (the punchline, the result, the reaction) before the end?

For each candidate return its index, a start and end inside its window, the score, and a \
one-sentence reason saying what happens in it. Use the transcript times to set the start \
and end: start at most 2 seconds before the line where the moment begins, and end just \
after the payoff, never in the middle of a sentence. Base the reason only on what you are \
given; do not invent events. When a candidate has no speech, you only have its stats: say \
so in the reason and score it cautiously. Windows can overlap: score each one on its own, \
because only the best of an overlapping set is used."""


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
        replay = f", most replayed: {s['replay']}" if "replay" in s else ""
        lines += [
            f"- index: {i}",
            f"  window: {c.start:.1f}-{c.end:.1f}s (found from {c.source})",
            f"  loudness z at peak: {s['loud_z']}, scene cuts: {s['cuts']}, "
            f"words/s: {s['words_per_s']}{replay}",
        ]
        timed = timed_lines(transcript.words_between(c.start, c.end))
        lines += ["  transcript:", *timed] if timed else ["  transcript: (no speech)"]
    return "\n".join(lines)


def timed_lines(words: list[Word]) -> list[str]:
    """Words as '[41.0s] text' lines, split at pauses, so Claude can place the start and end."""
    groups: list[list[Word]] = []
    for w in words:
        if groups and w.start - groups[-1][-1].end < PAUSE_GAP and len(groups[-1]) < LINE_MAX_WORDS:
            groups[-1].append(w)
        else:
            groups.append([w])
    return [f"    [{g[0].start:.1f}s] {' '.join(w.text.strip() for w in g)}" for g in groups]


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
            SYSTEM,
            build_prompt(job, candidates, transcript, clip_min, clip_max),
            Ranking,
            model=config.CLAUDE_MODEL_RANK,
            task="rank",
        ).clips
    except llm.LLMError as e:
        log.warning("ranker fell back to signal scores: %s", e)
        answer = []

    words = [w for s in transcript.segments for w in s.words]
    picks = [p for p in (_valid(r, candidates, words, clip_min, clip_max) for r in answer) if p]
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
    r: RankedClip, candidates: list[Candidate], words: list[Word], clip_min: float, clip_max: float
) -> tuple[float, float, float, str] | None:
    """Fit a pick to its candidate window: edges moved off words, a short pick stretched to
    clip_min. None if the index is bad or the length still doesn't fit."""
    if not 0 <= r.index < len(candidates):
        return None
    c = candidates[r.index]
    # an edge inside a word would cut it in half: start before that word, end after it
    start = next((w.start for w in words if w.start < r.start < w.end), r.start)
    end = next((w.end for w in words if w.start < r.end < w.end), r.end)
    start, end = max(start, c.start), min(end, c.end)
    if end - start < clip_min:  # keep Claude's start if there is room after it
        end = min(c.end, start + clip_min)
        start = max(c.start, end - clip_min)
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
