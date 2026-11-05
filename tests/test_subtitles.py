from clipper.models import Word
from clipper.render import subtitles as st


def words(*spec):
    return [Word(start=s, end=e, text=t) for s, e, t in spec]


SPEECH = words(
    (100.0, 100.4, "okay"), (100.4, 100.8, "they're"), (100.8, 101.2, "right"),
    (101.2, 101.6, "behind"), (101.6, 102.0, "me"),  # 5 words: 4 + 1
    (103.5, 104.0, "NO"), (104.0, 104.5, "WAY"),  # after a 1.5 s pause
    (130.0, 131.0, "outside"),  # past the clip end
)  # fmt: skip


def dialogues(ass):
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_group_words_by_count_and_pause_in_clip_time():
    blocks = st.group_words(SPEECH, clip_start=99.0, clip_end=120.0)
    assert [[w.text for w in b] for b in blocks] == [
        ["okay", "they're", "right", "behind"],
        ["me"],
        ["NO", "WAY"],
    ]
    assert (blocks[0][0].start, blocks[2][1].end) == (1.0, 5.5)


def test_one_event_per_word_with_no_gaps_and_active_word_in_yellow():
    blocks = st.group_words(SPEECH[:4], 99.0, 120.0)
    events = dialogues(st.build_ass(blocks, None))
    assert len(events) == 4
    assert events[0].startswith("Dialogue: 0,0:00:01.00,0:00:01.40,Caption,")
    assert ",0:00:01.40,0:00:01.80," in events[1]  # starts where the last one ended
    assert events[1].endswith("okay {\\c&H00FFFF&}they're{\\r} right behind")


def test_hook_spans_first_three_seconds_top_style():
    events = dialogues(st.build_ass([], "  He did NOT   see that "))
    assert events == ["Dialogue: 0,0:00:00.00,0:00:03.00,Hook,,0,0,0,,He did NOT see that"]


def test_ass_override_characters_are_neutralised():
    blocks = [words((0.0, 1.0, "{\\b1}lol\\N"))]
    assert dialogues(st.build_ass(blocks, "a {b} \\c"))[0].endswith(",a (b) /c")
    assert "(/b1)lol/N" in dialogues(st.build_ass(blocks, None))[0]


def test_nothing_to_show_gives_empty_file():
    assert st.build_ass([], None) == ""
    assert st.build_ass([], "   ") == ""
    assert "Hook" in st.build_ass([], "hook only")


def test_timestamps_roll_over_minutes_and_hours():
    assert st._ts(61.234) == "0:01:01.23" and st._ts(3725.5) == "1:02:05.50"
