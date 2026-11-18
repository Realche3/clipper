import subprocess
import wave

import numpy as np
import pytest

from clipper.analyze import candidates as cand
from clipper.analyze import signals as sig
from clipper.models import HeatPoint, Segment, Transcript, Word


def transcript(*words: tuple[float, float, str]) -> Transcript:
    ws = [Word(start=s, end=e, text=t) for s, e, t in words]
    return Transcript(segments=[Segment(start=ws[0].start, end=ws[-1].end, text="", words=ws)])


# ---------------------------------------------------------------- signals


def test_loudness_per_second_and_read_wav(tmp_path):
    sr = 16000
    quiet, loud = np.zeros(sr), 0.5 * np.sin(np.linspace(0, 2000, sr // 2))  # 1.5 s total
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((np.concatenate([quiet, loud]) * 32767).astype("<i2").tobytes())

    samples, rate = sig.read_wav(path)
    db = sig.loudness_per_second(samples, rate)
    assert rate == sr and len(db) == 2  # the partial last second counts
    assert db[0] == pytest.approx(-100) and db[1] > -20


def test_flat_signals_give_no_nans():
    flat = np.full(100, -30.0)
    assert not sig.zscore(flat).any()
    score = sig.combine(flat, np.zeros(100), np.zeros(100))
    assert not np.isnan(score).any() and not score.any()
    assert len(sig.zscore(np.array([]))) == 0


def test_silent_stretches_do_not_flatten_loudness_peaks():
    rng = np.random.default_rng(0)
    loud = -30 + rng.normal(0, 3, 600)
    loud[300] = -10  # the moment
    with_silence = loud.copy()
    with_silence[:60] = -100  # a silent intro (digital silence floor)
    assert sig.robust_z(with_silence)[300] == pytest.approx(sig.robust_z(loud)[300], rel=0.15)
    assert sig.robust_z(with_silence).min() == -3  # silence is capped, not an outlier
    assert sig.combine(with_silence, np.zeros(600), np.zeros(600)).argmax() in (299, 300, 301)


def test_cut_density_counts_a_centred_window():
    d = sig.cut_density([10.2, 11.0, 30.5, 99.0], n=40, window=5)
    assert d[10] == 2 and d[8] == 1 and d[13] == 1 and d[14] == 0
    assert d[30] == 1 and d.sum() == 15  # the cut past the end is ignored


def test_speech_counts_words_and_keyword_hits():
    filler = [(float(t), t + 0.5, "ok") for t in range(20, 30)]
    t = transcript((5.0, 5.3, "NO"), (5.3, 5.6, "WAY"), (5.7, 6.0, "bro!"), *filler)
    speech = sig.speech_per_second(t, n=60)
    # second 5: three words, "bro" keyword, "no way" phrase
    assert speech[5] == 3 + 1 + 1
    assert speech[20] == 1 and speech[40] == 0


def test_sparse_speech_counts_as_zero():
    t = transcript((5.0, 5.3, "bro"), (6.0, 6.3, "what"))
    assert not sig.speech_per_second(t, n=600).any()


def test_combine_weights_and_smooths():
    n = 50
    loud = np.zeros(n)
    loud[25] = 1.0
    score = sig.combine(loud, np.zeros(n), np.zeros(n))
    assert score.argmax() in (24, 25, 26)
    assert score[24] == pytest.approx(score[25]) == pytest.approx(score[26])  # 3 s average
    assert score[22] < 0  # z-scored, so quiet seconds sit below zero


def test_detect_cuts_finds_a_hard_cut(tmp_path):
    video = tmp_path / "cut.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=red:s=320x180:d=2:r=30",
            "-f", "lavfi", "-i", "color=blue:s=320x180:d=2:r=30",
            "-filter_complex", "[0][1]concat=n=2:v=1", str(video),
        ],
        check=True,
    )  # fmt: skip
    assert sig.detect_cuts(video) == [pytest.approx(2.0, abs=0.05)]


# ---------------------------------------------------------------- candidates


def test_pick_peaks_best_first_and_spaced():
    score = np.zeros(200)
    score[[50, 55, 100, 30, 190]] = [5, 4.9, 4, 3, 2]
    peaks = cand.pick_peaks(score, k=3, min_gap=20)
    assert peaks == [50, 100, 30]  # 55 is too close to 50
    assert all(abs(a - b) >= 20 for a in peaks for b in peaks if a != b)


def test_pauses_are_gap_midpoints():
    t = transcript((1.0, 1.5, "a"), (1.6, 2.0, "b"), (3.0, 3.5, "c"))
    assert cand.pauses(t) == [2.5]  # the 0.1 s gap is not a pause


def test_window_snaps_to_a_nearby_pause_only():
    # raw window 92-120; a pause at 90.5 is close, the one at 130 is not
    assert cand.make_window(100, [90.5, 130.0], 600, 15, 60) == (90.5, 120)


def test_window_respects_min_max_and_source_edges():
    assert cand.make_window(3, [], 600, 15, 60) == (0, 28)  # clipped at 0, length kept
    assert cand.make_window(595, [], 600, 15, 60) == (572, 600)  # shifted back from the end
    assert cand.make_window(100, [], 600, 30, 60) == (92, 122)  # stretched to clip_min
    assert cand.make_window(100, [], 600, 15, 20) == (92, 112)  # cut to clip_max
    assert cand.make_window(5, [], 10, 15, 60) == (0, 10)  # source shorter than clip_min


def test_find_candidates_have_stats():
    n = 300
    score = np.zeros(n)
    score[[60, 200]] = [2.0, 1.0]
    loud_z = np.arange(n) / 100
    t = transcript(*[(float(s), s + 0.4, "go") for s in range(55, 65)])
    out = cand.find_candidates(score, loud_z, [58.0, 61.0, 250.0], t, n, 15, 15, 60)

    assert [c.peak for c in out][:2] == [60, 200]
    first = out[0]
    assert (first.start, first.end) == (52, 80)
    assert first.stats == {"loud_z": 0.6, "cuts": 2, "words_per_s": round(10 / 28, 2)}


# ---------------------------------------------------------------- most replayed


def heat(*values: float, span: float = 10.0) -> list[HeatPoint]:
    return [HeatPoint(start=i * span, end=(i + 1) * span, value=v) for i, v in enumerate(values)]


def test_replay_per_second_ignores_the_always_high_first_span():
    replay = sig.replay_per_second(heat(1.0, 0.2, 0.9, 0.2), n=35)
    assert replay[5] == pytest.approx(0.55)  # first span -> the median, not 1.0
    assert replay[15] == 0.2 and replay[25] == 0.9 and replay[34] == 0.2
    assert not sig.replay_per_second([], n=35).any()


def test_most_replayed_moves_the_peak():
    n = 300
    loud = np.full(n, -30.0)
    loud[50] = -10
    quiet = np.zeros(n)
    assert sig.combine(loud, quiet, quiet).argmax() in (49, 50, 51)
    replay = sig.replay_per_second(heat(*([0.1] * 20), 1.0, *([0.1] * 9)), n)  # 200-210 s
    assert 199 <= sig.combine(loud, quiet, quiet, replay).argmax() <= 210
    # no graph: the score is unchanged
    plain = sig.combine(loud, quiet, quiet)
    assert np.array_equal(sig.combine(loud, quiet, quiet, np.zeros(n)), plain)


def test_candidate_stats_include_replay_when_there_is_a_graph():
    n = 300
    score = np.zeros(n)
    score[60] = 2.0
    replay = np.zeros(n)
    replay[70:75] = 0.8
    t = transcript((55.0, 55.4, "go"))
    out = cand.find_candidates(score, np.zeros(n), [], t, n, 1, 15, 60, replay)
    assert out[0].stats["replay"] == 0.8 and out[0].source == "signals"


def test_candidates_for_transcript_windows():
    n = 300
    score = np.zeros(n)
    score[130] = 1.5
    t = transcript((120.0, 120.4, "so"), (121.0, 121.4, "then"))
    (c,) = cand.candidates_for([(120.0, 150.0)], score, np.arange(n) / 100, [125.0], t)
    assert (c.start, c.end, c.peak, c.score, c.source) == (120.0, 150.0, 130, 1.5, "transcript")
    assert c.stats == {"loud_z": 1.3, "cuts": 1, "words_per_s": round(2 / 30, 2)}


def test_fit_window_keeps_length_and_source_bounds():
    assert cand.fit_window(100, 104, 600, 15, 60) == (100, 115)
    assert cand.fit_window(100, 300, 600, 15, 60) == (100, 160)
    assert cand.fit_window(590, 598, 600, 15, 60) == (585, 600)
