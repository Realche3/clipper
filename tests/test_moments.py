import wave

import pytest

from clipper import analyze, config, llm
from clipper.analyze import moments, signals
from clipper.analyze import rank as rk
from clipper.models import HeatPoint, Job, JobStatus, Segment, Transcript, Word

CLIP_MIN, CLIP_MAX = 15.0, 60.0
JOB = Job(id="j1", source_url="https://youtu.be/x", title="Storytime", context="funny stories")
# one word a second from 100 s to 160 s: plenty of speech, nothing loud
WORDS = [Word(start=float(t), end=t + 0.9, text=f"w{t}") for t in range(100, 160)]
TRANSCRIPT = Transcript(segments=[Segment(start=100, end=160, text="", words=WORDS)])


@pytest.fixture
def claude(monkeypatch):
    """`claude.answers[task]` is what ask_json returns for that task, or an exception to raise."""

    class Fake:
        def __init__(self):
            self.answers: dict[str, object] = {}
            self.calls: list[tuple[str, str]] = []

        def __call__(self, system, prompt, schema, *, task, **kwargs):
            self.calls.append((task, prompt))
            answer = self.answers.get(task, llm.LLMError("offline"))
            if isinstance(answer, Exception):
                raise answer
            return answer

    fake = Fake()
    monkeypatch.setattr(llm, "ask_json", fake)
    return fake


def moment(start, end):
    return moments.Moment(start=start, end=end)


def test_propose_fits_windows_and_drops_bad_ones(claude):
    claude.answers["moments"] = moments.Moments(
        moments=[
            moment(110, 140),  # fine as is
            moment(120, 124),  # too short: stretched to CLIP_MIN
            moment(100, 400),  # too long: cut to CLIP_MAX
            moment(150, 150),  # empty
            moment(900, 930),  # past the end of the source
            moment(130, 150),  # one more than asked for
        ]
    )
    windows = moments.propose(JOB, TRANSCRIPT, 600, 3, CLIP_MIN, CLIP_MAX)
    assert windows == [(110, 140), (120, 135), (100, 160)]

    task, prompt = claude.calls[0]
    assert task == "moments"
    assert "funny stories" in prompt and "Storytime" in prompt
    assert "Find up to 3 moments, each 15-60 seconds long." in prompt
    assert "[100.0s] w100 w101" in prompt  # the whole transcript, with times


def test_propose_returns_nothing_when_claude_fails(claude):
    assert moments.propose(JOB, TRANSCRIPT, 600, 3, CLIP_MIN, CLIP_MAX) == []


@pytest.fixture
def job(tmp_path, monkeypatch):
    """An ingested 200 s job: silent audio, no cuts, and TRANSCRIPT already on disk."""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(signals, "detect_cuts", lambda video: [])
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0\0" * 16000 * 200)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"")
    job = Job(
        id="j1",
        source_url="https://youtu.be/x",
        status=JobStatus.ingested,
        audio_file=audio,
        source_file=source,
        duration=200.0,
    )
    job_dir = Job.dir_for(tmp_path, job.id)
    job_dir.mkdir(parents=True)
    (job_dir / "transcript.json").write_text(TRANSCRIPT.model_dump_json(), encoding="utf-8")
    return job


def test_stage_ranks_transcript_moments_with_signal_candidates(claude, job):
    claude.answers["moments"] = moments.Moments(moments=[moment(110, 140)])
    job = analyze.run(job)

    rank_prompt = next(prompt for task, prompt in claude.calls if task == "rank")
    assert "window: 110.0-140.0s (found from transcript)" in rank_prompt
    assert "(found from signals)" in rank_prompt
    assert job.clips  # the ranker was offline: signal candidates filled in


def test_stage_skips_moments_when_speech_is_sparse(claude, job):
    sparse = Transcript(segments=[Segment(start=5, end=6, text="", words=WORDS[:2])])
    job.transcript_file = Job.dir_for(config.WORKSPACE, job.id) / "transcript.json"
    job.transcript_file.write_text(sparse.model_dump_json(), encoding="utf-8")
    analyze.run(job)
    assert [task for task, _ in claude.calls] == ["rank"]


def test_stage_shows_most_replayed_to_the_ranker(claude, job):
    job.heatmap = [
        HeatPoint(start=i * 20.0, end=(i + 1) * 20.0, value=v)
        for i, v in enumerate([1.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.9, 0.1, 0.1, 0.1])
    ]
    analyze.run(job)
    rank_prompt = next(prompt for task, prompt in claude.calls if task == "rank")
    assert "most replayed: 0.9" in rank_prompt
    # the audio is silent, so the best candidate is the most replayed stretch (120-140 s)
    first = rank_prompt.split("- index: 0")[1].split("- index: 1")[0]
    start, end = (float(x) for x in first.split("window: ")[1].split("s (")[0].split("-"))
    assert start < 140 and end > 120


def test_no_replay_stat_without_a_graph(claude, job):
    analyze.run(job)
    assert "most replayed" not in next(p for task, p in claude.calls if task == "rank")
    assert rk.SYSTEM.count("most replayed") == 1  # explained once, in the instructions
