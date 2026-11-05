"""ffmpeg/ffprobe helpers for ingest."""

from __future__ import annotations

import subprocess
from pathlib import Path


def probe_duration(path: Path) -> float:
    """Container duration in seconds."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def extract_audio(src: Path, wav: Path) -> None:
    """16 kHz mono PCM wav, shared by transcription and loudness analysis."""
    tmp = wav.with_name(wav.stem + ".tmp.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000"]
        + ["-c:a", "pcm_s16le", str(tmp)],
        check=True,
    )
    tmp.replace(wav)  # never leave a half-written audio.wav that a re-run would skip
