import wave
from types import SimpleNamespace as NS

import pytest

from clipper import analyze, config, llm
from clipper.analyze import signals
from clipper.analyze import transcribe as tr
from clipper.models import Job, JobStatus, Transcript


def w(start, end, word):
    return NS(start=start, end=end, word=word, probability=0.9)


SEGMENTS = [
    NS(
        start=42.5,
        end=44.2,
        text=" NO WAY bro",
        words=[w(42.5, 43.0, " NO"), w(43.0, 43.6, " WAY"), w(43.7, 44.2, " bro")],
    ),
    # "YouTube-Kanal." arrives as " YouTube" + "-Kanal."; " " is an empty token
    NS(
        start=50.0,
        end=51.5,
        text=" YouTube-Kanal.",
        words=[w(50.0, 50.8, " YouTube"), w(50.8, 51.5, "-Kanal."), w(51.5, 51.5, " ")],
    ),
]


@pytest.fixture
def fake_whisper(monkeypatch):
    calls = {"init": [], "transcribe": []}

    class FakeModel:
        def __init__(self, name, **kwargs):
            calls["init"].append((name, kwargs))

        def transcribe(self, audio, **params):
            calls["transcribe"].append((audio, params))
            return iter(SEGMENTS), NS(language="en", duration=60.0)

    monkeypatch.setattr(tr, "WhisperModel", FakeModel)
    return calls


def test_transcribe_converts_to_transcript(fake_whisper, tmp_path):
    t = tr.transcribe(tmp_path / "audio.wav", "small")

    assert t.language == "en"
    assert t.segments[0].text == "NO WAY bro"
    assert t.text_between(42.5, 44.2) == "NO WAY bro"
    name, kwargs = fake_whisper["init"][0]
    assert name == "small" and kwargs["compute_type"] == "int8"
    params = fake_whisper["transcribe"][0][1]
    assert params["word_timestamps"] and params["vad_filter"]
    assert params["condition_on_previous_text"] is False


def test_fragments_merge_into_previous_word(fake_whisper, tmp_path):
    words = tr.transcribe(tmp_path / "audio.wav", "small").segments[1].words
    assert [x.text for x in words] == ["YouTube-Kanal."]
    assert (words[0].start, words[0].end) == (50.0, 51.5)


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(signals, "detect_cuts", lambda video: [10.0])

    def no_claude(*args, **kwargs):
        raise llm.LLMError("offline")

    monkeypatch.setattr(llm, "ask_json", no_claude)  # the ranker falls back to signals
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as f:  # 60 s of silence; the fake model never reads it
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0\0" * 16000 * 60)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"")
    return Job(
        id="j1",
        source_url="https://youtu.be/x",
        status=JobStatus.ingested,
        audio_file=audio,
        source_file=source,
        duration=60.0,
    )


def test_stage_writes_transcript_and_skips_rerun(fake_whisper, job, caplog):
    caplog.set_level("INFO")
    job = analyze.run(job)
    assert "candidate " in caplog.text
    saved = Transcript.model_validate_json(job.transcript_file.read_text(encoding="utf-8"))
    assert saved.segments[0].words[1].text == "WAY"
    assert job.status == JobStatus.analyzed and job.clips

    analyze.run(job)
    assert len(fake_whisper["transcribe"]) == 1
    analyze.run(job, force=True)
    assert len(fake_whisper["transcribe"]) == 2


def test_stage_needs_audio(job):
    job.audio_file = None
    with pytest.raises(RuntimeError, match="ingest"):
        analyze.run(job)
