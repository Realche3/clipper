"""Speech to text with word timestamps (faster-whisper). Spec: docs/ARCHITECTURE.md §5.2.1."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

from clipper.models import Segment, Transcript, Word

log = logging.getLogger(__name__)

# condition_on_previous_text=False stops runaway repeated lines on noisy game audio.
PARAMS = {
    "beam_size": 5,
    "vad_filter": True,
    "condition_on_previous_text": False,
    "word_timestamps": True,
}


def transcribe(audio: Path, model_name: str) -> Transcript:
    started = time.monotonic()
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, info = model.transcribe(str(audio), **PARAMS)
    transcript = Transcript(
        language=info.language,
        segments=[_segment(s) for s in segments],  # decoding happens while iterating
    )
    took = time.monotonic() - started
    log.info(
        "transcribed %.0fs of audio in %.0fs with whisper %s (%.1fx realtime)",
        info.duration,
        took,
        model_name,
        info.duration / max(took, 1e-6),
    )
    return transcript


def _segment(seg: Any) -> Segment:
    return Segment(
        start=seg.start, end=seg.end, text=seg.text.strip(), words=merge_fragments(seg.words or [])
    )


def merge_fragments(words: list[Any]) -> list[Word]:
    """Glue sub-word pieces back onto the word before them.

    faster-whisper starts every real word with a space; a piece without one
    ("-Kanal", ".200") belongs to the previous word. Adapted from OpenShorts'
    merge_continuation_words (MIT).
    """
    merged: list[Word] = []
    for w in words:
        if merged and w.word and not w.word.startswith(" "):
            merged[-1].text += w.word
            merged[-1].end = w.end
        else:
            merged.append(Word(start=w.start, end=w.end, text=w.word))
    for m in merged:
        m.text = m.text.strip()
    return [m for m in merged if m.text]
