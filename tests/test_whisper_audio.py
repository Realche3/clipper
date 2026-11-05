"""Regression: faster-whisper's own audio decoding must work with the installed PyAV.

The transcribe tests mock WhisperModel, so they never touch PyAV. This test runs the real
decoder (no model download) and catches version clashes like PyAV 19 removing
`metadata_errors`, which crashed real runs.
"""

import subprocess

from faster_whisper import decode_audio


def test_faster_whisper_can_decode_a_wav(tmp_path):
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi"]
        + ["-i", "sine=frequency=440:duration=1", "-ar", "16000", "-ac", "1", str(wav)],
        check=True,
    )
    audio = decode_audio(str(wav), sampling_rate=16000)
    assert audio.shape[0] == 16000
