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
        pick(6, 241, 245, 0.8),  # too short: stretched to CLIP_MIN inside its window
    ]
    clips = run_rank()
    assert (clips[-1].start, clips[-1].end, clips[-1].score) == (280, 308, 1.0)
    assert (clips[-2].start, clips[-2].end, clips[-2].score) == (241, 256, 0.8)
    # the other three come from the best signal candidates, flagged as such
    assert [c.start for c in clips[:3]] == [0, 40, 80]
    assert all(c.score == 0 and "signals" in c.reason for c in clips[:3])


def test_short_pick_at_window_end_stretches_backwards(claude):
    claude.answer = [pick(1, 60, 68, 0.9)]  # window 40-68: no room after, so start moves back
    clip = next(c for c in run_rank() if c.score == 0.9)
    assert (clip.start, clip.end) == (53, 68)


def test_pick_edges_move_off_words(claude):
    words = [Word(start=41, end=42, text="NO"), Word(start=60.0, end=60.8, text="way")]
    transcript = Transcript(segments=[Segment(start=41, end=60.8, text="", words=words)])
    claude.answer = [pick(1, 41.5, 60.3, 0.9)]  # both edges land inside a word
    clips = rk.rank(JOB, CANDIDATES, transcript, N, CLIP_MIN, CLIP_MAX)
    clip = next(c for c in clips if c.score == 0.9)
    assert (clip.start, clip.end) == (41.0, 60.8)


def test_llm_error_falls_back_to_signal_ranking(claude):
    claude.answer = llm.LLMError("refusal")
    clips = run_rank()
    assert [c.start for c in clips] == [0, 40, 80, 120, 160]


def test_prompt_has_context_stats_and_transcript(claude):
    rk.rank(JOB, CANDIDATES, TRANSCRIPT, N, CLIP_MIN, CLIP_MAX)
    prompt = claude.prompts[0]
    assert "police fails" in prompt and "GTA 6 chaos" in prompt
    assert "index: 1" in prompt and "window: 40.0-68.0s" in prompt and "scene cuts: 2" in prompt
    assert "[41.0s] NO" in prompt and "transcript: (no speech)" in prompt


def test_prompt_transcript_breaks_lines_at_pauses(claude):
    words = [
        Word(start=41.0, end=41.4, text="oh"),
        Word(start=41.5, end=42.0, text="no"),
        Word(start=45.0, end=45.5, text="RUN"),
    ]
    transcript = Transcript(segments=[Segment(start=41, end=45.5, text="", words=words)])
    rk.rank(JOB, CANDIDATES, transcript, N, CLIP_MIN, CLIP_MAX)
    lines = [line.strip() for line in claude.prompts[-1].splitlines()]
    assert "[41.0s] oh no" in lines and "[45.0s] RUN" in lines
