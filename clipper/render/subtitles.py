"""Burned-in subtitles and hook as an ASS file. Spec: docs/ARCHITECTURE.md §5.4.

Karaoke look: one event per word, back to back, with the spoken word in yellow (the idea is
from OpenShorts' generate_ass, MIT). Times are relative to the clip start.
"""

from __future__ import annotations

from clipper.models import Word

MAX_WORDS = 4
MAX_GAP = 0.8  # a longer pause between words starts a new line
HOOK_SECONDS = 3.0
YELLOW = "&H00FFFF&"  # ASS colours are BGR

HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, \
Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,\
-1,0,0,0,100,100,0,0,1,6,0,2,60,60,500,1
Style: Hook,Arial,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,\
-1,0,0,0,100,100,0,0,1,8,0,8,60,60,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def group_words(
    words: list[Word],
    clip_start: float,
    clip_end: float,
    max_words: int = MAX_WORDS,
    max_gap: float = MAX_GAP,
) -> list[list[Word]]:
    """Words inside the clip, shifted to clip time, in lines of up to `max_words`."""
    blocks: list[list[Word]] = []
    for w in words:
        if w.start < clip_start or w.end > clip_end:
            continue
        w = Word(start=w.start - clip_start, end=w.end - clip_start, text=w.text)
        last = blocks[-1] if blocks else None
        if last and len(last) < max_words and w.start - last[-1].end <= max_gap:
            last.append(w)
        else:
            blocks.append([w])
    return blocks


def build_ass(blocks: list[list[Word]], hook_text: str | None) -> str:
    """The whole .ass file, or "" when there is nothing to show."""
    events = []
    if hook_text and hook_text.strip():
        events.append(_event(0, HOOK_SECONDS, "Hook", _clean(hook_text)))
    for block in blocks:
        texts = [_clean(w.text) for w in block]
        for i, w in enumerate(block):
            end = block[i + 1].start if i + 1 < len(block) else w.end  # no gaps inside a line
            line = " ".join(
                f"{{\\c{YELLOW}}}{t}{{\\r}}" if j == i else t for j, t in enumerate(texts)
            )
            events.append(_event(w.start, end, "Caption", line))
    return HEADER + "\n".join(events) + "\n" if events else ""


def _clean(text: str) -> str:
    """ASS treats {} as override blocks and \\ as a tag; swap them for harmless lookalikes."""
    return " ".join(text.replace("{", "(").replace("}", ")").replace("\\", "/").split())


def _event(start: float, end: float, style: str, text: str) -> str:
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}"


def _ts(t: float) -> str:
    cs = round(max(t, 0) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"
