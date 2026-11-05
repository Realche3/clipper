import pytest

from clipper import llm
from clipper.analyze import rank as rk
from clipper.analyze.candidates import Candidate
from clipper.models import Job, Segment, Transcript, Word

N, CLIP_MIN, CLIP_MAX = 5, 15.0, 60.0
STATS = {"loud_z": 1.5, "cuts": 2, "words_per_s": 0.5}
# 8 candidates, best signal first, windows 28 s long and 40 s apart (no overlaps)
CANDIDATES = [Candidate(i * 40.0, i * 40.0 + 28, i * 40 + 8, 1 - i / 10, STATS) for i in range(8)]
TRANSCRIPT = Transcript(
    segments=[Segment(start=41, end=42, text="NO WAY", words=[Word(start=41, end=42, text="NO")])]
)
JOB = Job(id="j1", source_url="https://youtu.be/x", title="GTA 6 chaos", context="police fails")


def pick(index, start, end, score, reason="big moment"):
    return rk.RankedClip(index=index, start=start, end=end, score=score, reason=reason)


@pytest.fixture
def claude(monkeypatch):
    """Set `claude.answer` to a list of picks, or to an exception to raise."""

    class Fake:
        answer: object = []
        prompts: list[str] = []

        def __call__(self, system, prompt, schema, **kwargs):
            self.prompts.append(prompt)
            if isinstance(self.answer, Exception):
                raise self.answer
            return schema(clips=self.answer)

    fake = Fake()
    monkeypatch.setattr(llm, "ask_json", fake)
    return fake


def run_rank():
    clips = rk.rank(JOB, CANDIDATES, TRANSCRIPT, N, CLIP_MIN, CLIP_MAX)
    assert len(clips) == N
    for c in clips:
        assert CLIP_MIN <= c.duration <= CLIP_MAX and 0 <= c.score <= 1
    assert [c.id for c in clips] == ["c01", "c02", "c03", "c04", "c05"]
    assert [c.start for c in clips] == sorted(c.start for c in clips)
    return clips


def test_good_answer_takes_claudes_top_n(claude):
    claude.answer = [pick(i, i * 40.0 + 2, i * 40.0 + 25, score=i / 10) for i in range(8)]
    clips = run_rank()
    # Claude liked the later candidates best: indexes 3..7
    assert [c.start for c in clips] == [122, 162, 202, 242, 282]
    assert clips[-1].score == pytest.approx(0.7) and clips[-1].reason == "big moment"


def test_bad_answer_is_cleaned_and_filled(claude):
    claude.answer = [
        pick(7, 270, 400, 1.7),  # runs past its window: clamped to 280-308, score to 1.0
        pick(7, 281, 306, 0.9),  # duplicate of the above: dropped (overlap)
        pick(99, 0, 20, 0.9),  # no such candidate
        pick(6, 241, 245, 0.8),  # too short
    ]
    clips = run_rank()
    assert (clips[-1].start, clips[-1].end, clips[-1].score) == (280, 308, 1.0)
    # the other four come from the best signal candidates, flagged as such
    assert [c.start for c in clips[:4]] == [0, 40, 80, 120]
    assert all(c.score == 0 and "signals" in c.reason for c in clips[:4])


def test_llm_error_falls_back_to_signal_ranking(claude):
    claude.answer = llm.LLMError("refusal")
    clips = run_rank()
    assert [c.start for c in clips] == [0, 40, 80, 120, 160]


def test_prompt_has_context_stats_and_transcript(claude):
    rk.rank(JOB, CANDIDATES, TRANSCRIPT, N, CLIP_MIN, CLIP_MAX)
    prompt = claude.prompts[0]
    assert "police fails" in prompt and "GTA 6 chaos" in prompt
    assert "index: 1" in prompt and "window: 40.0-68.0s" in prompt and "scene cuts: 2" in prompt
    assert "transcript: NO" in prompt and "transcript: (no speech)" in prompt
