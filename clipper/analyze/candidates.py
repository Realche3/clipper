"""Candidate clip windows from the combined score. Spec: docs/ARCHITECTURE.md §5.2.3."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np

from clipper.models import Transcript

PRE_ROLL = 8.0
POST_ROLL = 20.0
MIN_GAP = 20
PAUSE_GAP = 0.3  # a silence between words at least this long counts as a pause
SNAP_RADIUS = 3.0  # only snap to a pause this close; further away, keep the raw edge


@dataclass
class Candidate:
    start: float
    end: float
    peak: int
    score: float
    stats: dict[str, float] = field(default_factory=dict)


def pick_peaks(score: np.ndarray, k: int, min_gap: int = MIN_GAP) -> list[int]:
    """Top-k seconds by score, best first, each at least `min_gap` s from the ones before."""
    picked: list[int] = []
    for i in np.argsort(-score, kind="stable"):
        if all(abs(int(i) - p) >= min_gap for p in picked):
            picked.append(int(i))
            if len(picked) == k:
                break
    return picked


def pauses(transcript: Transcript) -> list[float]:
    """Midpoints of the silences between consecutive words."""
    words = [w for s in transcript.segments for w in s.words]
    return [(a.end + b.start) / 2 for a, b in pairwise(words) if b.start - a.end >= PAUSE_GAP]


def _snap(t: float, points: list[float]) -> float:
    near = min(points, key=lambda p: abs(p - t), default=None)
    return near if near is not None and abs(near - t) <= SNAP_RADIUS else t


def make_window(
    peak: float, points: list[float], duration: float, clip_min: float, clip_max: float
) -> tuple[float, float]:
    """peak −8 s → peak +20 s, edges snapped to nearby pauses, length kept in
    [clip_min, clip_max] and the window kept inside the source."""
    start = _snap(peak - PRE_ROLL, points)
    end = _snap(peak + POST_ROLL, points)
    end = min(max(end, start + clip_min), start + clip_max)
    if end > duration:  # shift back rather than shorten
        start, end = start - (end - duration), duration
    if start < 0:
        start, end = 0.0, min(end - start, duration)
    return round(start, 2), round(end, 2)


def find_candidates(
    score: np.ndarray,
    loud_z: np.ndarray,
    cuts: list[float],
    transcript: Transcript,
    duration: float,
    k: int,
    clip_min: float,
    clip_max: float,
) -> list[Candidate]:
    """The best k windows, best first, with the stats the ranker shows Claude."""
    points = pauses(transcript)
    out = []
    for peak in pick_peaks(score, k):
        start, end = make_window(peak, points, duration, clip_min, clip_max)
        words = len(transcript.words_between(start, end))
        stats = {
            "loud_z": round(float(loud_z[peak]), 2),
            "cuts": sum(start <= c < end for c in cuts),
            "words_per_s": round(words / max(end - start, 1e-6), 2),
        }
        out.append(Candidate(start, end, peak, round(float(score[peak]), 3), stats))
    return out
