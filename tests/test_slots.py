from datetime import datetime, time, timedelta

import pytest

from clipper.publish.slots import next_slot, parse_window

WINDOW = (time(10, 0), time(23, 30))
HOUR = timedelta(minutes=60)


def slot(now, taken=(), interval=HOUR, cap=5, window=WINDOW):
    return next_slot(now, list(taken), interval=interval, window=window, daily_cap=cap)


def dt(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm)


def test_inside_window_with_nothing_taken_is_now_rounded_up():
    assert slot(datetime(2026, 11, 19, 14, 7, 30)) == dt(19, 14, 8)


def test_before_window_moves_to_window_start():
    assert slot(dt(19, 6)) == dt(19, 10)


def test_after_window_moves_to_next_day():
    assert slot(dt(19, 23, 45)) == dt(20, 10)


def test_spaces_after_taken_slot():
    assert slot(dt(19, 14), taken=[dt(19, 14)]) == dt(19, 15)


def test_chains_through_several_taken_slots():
    taken = [dt(19, 14), dt(19, 15), dt(19, 16)]
    assert slot(dt(19, 14), taken) == dt(19, 17)


def test_fills_gap_left_by_cancellation():
    taken = [dt(19, 10), dt(19, 13)]  # 11:00 and 12:00 were cancelled
    assert slot(dt(19, 10), taken) == dt(19, 11)


def test_gap_too_small_is_skipped():
    taken = [dt(19, 10), dt(19, 11, 30)]  # only 90 min between them, need 60 on each side
    assert slot(dt(19, 10), taken) == dt(19, 12, 30)


def test_future_slot_blocks_candidate_that_is_too_close():
    assert slot(dt(19, 14), taken=[dt(19, 14, 30)]) == dt(19, 15, 30)


def test_daily_cap_rolls_to_next_day():
    taken = [dt(19, 10), dt(19, 11)]
    assert slot(dt(19, 12), taken, cap=2) == dt(20, 10)


def test_slot_pushed_past_window_end_rolls_to_next_day():
    assert slot(dt(19, 23), taken=[dt(19, 23)]) == dt(20, 10)


def test_interval_applies_across_midnight():
    window = (time(0, 0), time(23, 59))
    assert slot(dt(19, 23, 50), taken=[dt(19, 23, 50)], window=window) == dt(20, 0, 50)


def test_invalid_cap_raises():
    with pytest.raises(ValueError):
        slot(dt(19, 12), cap=0)


def test_parse_window():
    assert parse_window("10:00-23:30") == WINDOW
    assert parse_window(" 09:15 - 18:00 ") == (time(9, 15), time(18, 0))
    for bad in ("10-", "23:00-10:00", "nonsense"):
        with pytest.raises(ValueError):
            parse_window(bad)
