from pathlib import Path

import pytest

from clipper import config, llm
from clipper.captions import generate
from clipper.captions.generate import CaptionSet, ClipCaption
from clipper.models import Job, JobStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def job() -> Job:
    return Job.model_validate_json((FIXTURES / "sample_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def sounds_file(tmp_path, monkeypatch):
    f = tmp_path / "sounds.yaml"
    f.write_text(
        "sounds:\n"
        "  - {name: Hype A, vibe: hype}\n"
        "  - {name: Hype B, vibe: hype}\n"
        "  - {name: Funny A, vibe: funny}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "TRENDING_SOUNDS_FILE", f)
    monkeypatch.setattr(config, "TRENDING_SOUNDS_JSON", tmp_path / "none.json")  # pinned only
    monkeypatch.setattr(config, "BASE_HASHTAGS", ["#gta6", "#gaming"])
    return f


def _fake_claude(monkeypatch, result=None, error=None):
    calls = []

    def fake(system, prompt, schema, **kw):
        calls.append((system, prompt))
        if error:
            raise error
        return result

    monkeypatch.setattr(llm, "ask_json", fake)
    return calls


def _cap(id, vibe="hype", **kw):
    base = dict(
        id=id,
        hook_options=["He did NOT see that coming", "Wait for the crash", "5 stars, zero chance"],
        hook_text="He did NOT see that coming",
        title="Crash escape",
        caption="Cops had no chance 😂 would you have made it?",
        hashtags=["#GTA6Chase", "vicecity", "#gta6"],
        vibe=vibe,
        sound=None,
    )
    return ClipCaption(**{**base, **kw})


def test_fills_every_clip_from_claude(monkeypatch, job):
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02", vibe="funny")]))
    out = generate.run(job)

    assert out.status == JobStatus.captioned
    c1, c2 = out.clips
    assert c1.hook_text == "He did NOT see that coming"
    assert c1.caption.startswith("Cops had no chance")
    assert c1.hashtags == ["#gta6", "#gaming", "#gta6chase", "#vicecity"]  # base first, deduped
    assert c1.suggested_sound == "Hype A"
    assert c2.vibe == "funny" and c2.suggested_sound == "Funny A"

    # one call per job, with transcript text for each clip in the prompt
    assert len(calls) == 1
    system, prompt = calls[0]
    assert "#gta6 #gaming" in system
    assert "NO WAY bro" in prompt and "get in the heli" in prompt


def test_prompt_includes_context_and_description(monkeypatch, job):
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    generate.run(job)
    system, prompt = calls[0]
    assert "Poster's note: GTA 6 launch week, police chase fails" in prompt
    assert "escape 5 stars in Vice City" in prompt
    assert "GTA" not in system  # the prompt itself is niche-agnostic


def test_no_base_hashtags_means_claude_picks_all(monkeypatch, job):
    monkeypatch.setattr(config, "BASE_HASHTAGS", [])
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    out = generate.run(job)
    assert "added automatically" not in calls[0][0]
    assert out.clips[0].hashtags == ["#gta6chase", "#vicecity", "#gta6"]


def test_fallback_uses_context_when_no_title(monkeypatch, job):
    job.title = None
    _fake_claude(monkeypatch, error=llm.LLMError("down"))
    assert generate.run(job).clips[0].title == "GTA 6 launch week, police chase fails"


def test_fallback_hook_stops_before_a_bracket_in_the_title(monkeypatch, job):
    job.title = "NEW GTA 6 GAMEPLAY (THESE LEAKS ARE INSANE!)"
    _fake_claude(monkeypatch, error=llm.LLMError("down"))
    assert generate.run(job).clips[0].hook_text == "NEW GTA 6 GAMEPLAY"


def test_falls_back_when_claude_fails(monkeypatch, job):
    _fake_claude(monkeypatch, error=llm.LLMError("down"))
    out = generate.run(job)
    assert out.status == JobStatus.captioned
    for c in out.clips:
        assert c.title == "GTA 6 - Insane Police Chase"
        assert c.hashtags == ["#gta6", "#gaming"]
        assert c.vibe == "hype" and c.suggested_sound
    # each clip keeps a caption of its own: the ranker's reason for picking it
    assert [c.caption for c in out.clips] == [
        "Loud crash + streamer yells 'no way'",
        "Helicopter escape, fast scene cuts",
    ]


def test_fallback_without_a_ranker_reason_numbers_the_clips(monkeypatch, job):
    for c in job.clips:  # what the ranker leaves when Claude was down there too
        c.score, c.reason = 0.0, "Picked by audio/video signals (loudness z 1.5)."
    _fake_claude(monkeypatch, error=llm.LLMError("down"))
    assert [c.caption for c in generate.run(job).clips] == [
        "GTA 6 - Insane Police Chase (part 1 of 2)",
        "GTA 6 - Insane Police Chase (part 2 of 2)",
    ]


def test_missing_clip_in_answer_gets_fallback(monkeypatch, job):
    _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01")]))
    out = generate.run(job)
    assert out.clips[0].title == "Crash escape"
    assert out.clips[1].title == "GTA 6 - Insane Police Chase"


def test_skips_when_already_captioned_unless_forced(monkeypatch, job):
    for c in job.clips:
        c.caption = "existing"
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    assert generate.run(job).clips[0].caption == "existing"
    assert calls == []
    assert generate.run(job, force=True).clips[0].caption != "existing"


def test_missing_transcript_still_works(monkeypatch, job):
    job.transcript_file = Path("does/not/exist.json")
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    generate.run(job)
    assert "(no speech)" in calls[0][1]


# ---- normalizers


def test_clean_hook_limits_words_and_strips_hashtags():
    assert generate.clean_hook("one two three #tag four five six seven") == (
        "one two three four five six"
    )


def test_clean_hook_keeps_one_emoji_at_the_end():
    assert generate.clean_hook("He \U0001f440 did NOT see that \U0001f602") == (
        "He did NOT see that \U0001f440"
    )
    assert generate.clean_hook("one two three four five six seven \U0001f602") == (
        "one two three four five six \U0001f602"
    )
    assert generate.clean_hook("\U0001f602") == ""


def test_pick_hook_does_not_count_an_emoji_as_a_word():
    long_with_emoji = "one two three four five six \U0001f602"
    assert generate.pick_hook(long_with_emoji, ["short"]) == long_with_emoji


def test_long_hook_is_replaced_by_an_option_that_fits(monkeypatch, job):
    long = "He really did not see that one coming at all"
    caps = [_cap("c01", hook_text=long, hook_options=[long, "Wait for the crash"]), _cap("c02")]
    _fake_claude(monkeypatch, CaptionSet(clips=caps))
    assert generate.run(job).clips[0].hook_text == "Wait for the crash"


def test_clean_caption_trims_at_word_boundary():
    text = "word " * 60
    out = generate.clean_caption(text)
    assert len(out) <= generate.CAPTION_MAX_CHARS
    assert out.endswith("…") and not out.endswith(" …")


def test_clean_caption_removes_hashtags():
    assert generate.clean_caption("insane #gta6 escape") == "insane escape"


def test_merge_hashtags_caps_the_total():
    # Instagram allows 5 hashtags per post; base tags count towards it
    out = generate.merge_hashtags(["#gta6", "#gaming"], [f"#t{i}" for i in range(10)] + ["", "#"])
    assert out == ["#gta6", "#gaming", "#t0", "#t1", "#t2"]
    assert len(generate.merge_hashtags([f"#b{i}" for i in range(8)], ["#x"])) == 5


# ---- sound picks


def test_prompt_lists_numbered_sounds(monkeypatch, job):
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    generate.run(job)
    prompt = calls[0][1]
    assert "1. Hype A (tiktok, hype)" in prompt and "3. Funny A (tiktok, funny)" in prompt


def test_claude_sound_pick_is_used(monkeypatch, job):
    _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01", sound=3), _cap("c02", sound=2)]))
    out = generate.run(job)
    assert [c.suggested_sound for c in out.clips] == ["Funny A", "Hype B"]


@pytest.mark.parametrize("bad", [0, 4, 99, -1])
def test_out_of_range_pick_falls_back_to_vibe(monkeypatch, job, bad):
    _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01", sound=bad), _cap("c02")]))
    assert generate.run(job).clips[0].suggested_sound == "Hype A"


def test_no_sounds_at_all(monkeypatch, job, sounds_file):
    sounds_file.write_text("sounds: []", encoding="utf-8")
    calls = _fake_claude(monkeypatch, CaptionSet(clips=[_cap("c01"), _cap("c02")]))
    out = generate.run(job)
    assert "(none)" in calls[0][1]
    assert out.clips[0].suggested_sound is None
