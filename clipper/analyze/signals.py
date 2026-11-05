"""Per-second highlight signals. Spec: docs/ARCHITECTURE.md §5.2.2.

Everything here is a pure numpy function except `read_wav` and `detect_cuts`, which read files.
Index i of every array covers second [i, i+1) of the source.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path

import numpy as np

from clipper.models import Transcript

WEIGHTS = (0.5, 0.3, 0.2)  # loud, cuts, speech
SMOOTH_SECONDS = 3
CUT_WINDOW = 5
# Reactions that mark a moment. Phrases are matched on the joined, lowercased words.
KEYWORDS = {"bro", "what", "wow", "omg", "yo", "dude", "wait", "insane", "haha", "lol", "lmao"}
PHRASES = ("no way", "oh my god", "let's go", "what the")
# OpenShorts' "speech is sparse" rule: below this, speech is noise, not signal.
SPARSE_MIN_WORDS = 8
SPARSE_MIN_WPM = 5.0


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """16-bit PCM wav (what ingest writes) -> float samples in [-1, 1], sample rate."""
    with wave.open(str(path), "rb") as f:
        sr, channels, frames = f.getframerate(), f.getnchannels(), f.readframes(f.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sr


def loudness_per_second(samples: np.ndarray, sr: int) -> np.ndarray:
    """RMS level in dB for each second (the last partial second counts as one)."""
    n = int(np.ceil(len(samples) / sr))
    padded = np.zeros(n * sr, dtype=np.float32)
    padded[: len(samples)] = samples
    rms = np.sqrt(np.mean(padded.reshape(n, sr) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-5))  # floor at -100 dB (digital silence)


def zscore(x: np.ndarray) -> np.ndarray:
    std = x.std() if len(x) else 0.0
    return (x - x.mean()) / std if std > 0 else np.zeros_like(x, dtype=float)


def detect_cuts(video: Path) -> list[float]:
    """Scene-cut times in seconds (PySceneDetect ContentDetector on a ~360p downscale)."""
    from scenedetect import ContentDetector, SceneManager, open_video

    stream = open_video(str(video))
    manager = SceneManager()
    manager.auto_downscale = False
    manager.downscale = max(1, stream.frame_size[1] // 360)
    manager.add_detector(ContentDetector())
    manager.detect_scenes(stream)
    return [start.seconds for start, _ in manager.get_scene_list()[1:]]


def cut_density(cuts: list[float], n: int, window: int = CUT_WINDOW) -> np.ndarray:
    """Number of cuts in a centred `window`-second window around each second."""
    hits = np.bincount([int(c) for c in cuts if 0 <= c < n], minlength=n).astype(float)
    return np.convolve(hits, np.ones(window), mode="same")


def speech_is_sparse(transcript: Transcript, duration: float) -> bool:
    words = sum(len(s.words) for s in transcript.segments)
    return words < SPARSE_MIN_WORDS or words / max(duration / 60, 1e-6) < SPARSE_MIN_WPM


def speech_per_second(transcript: Transcript, n: int) -> np.ndarray:
    """Words per second plus one per reaction keyword or phrase. All zeros if speech is sparse."""
    speech = np.zeros(n)
    if speech_is_sparse(transcript, n):
        return speech
    words = [w for s in transcript.segments for w in s.words]
    norm = [re.sub(r"[^\w']", "", w.text.lower()) for w in words]
    for i, w in enumerate(words):
        t = int(w.start)
        if not 0 <= t < n:
            continue
        speech[t] += 1 + (norm[i] in KEYWORDS)
        following = " ".join(norm[i : i + 3])
        speech[t] += sum(following.startswith(p + " ") or following == p for p in PHRASES)
    return speech


def combine(loud: np.ndarray, cuts: np.ndarray, speech: np.ndarray) -> np.ndarray:
    """Weighted sum of the z-scored signals, smoothed with a 3 s moving average."""
    a, b, c = WEIGHTS
    raw = a * zscore(loud) + b * zscore(cuts) + c * zscore(speech)
    return np.convolve(raw, np.ones(SMOOTH_SECONDS) / SMOOTH_SECONDS, mode="same")
