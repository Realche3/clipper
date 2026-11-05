import subprocess
import wave

import numpy as np
import pytest

from clipper.analyze import candidates as cand
from clipper.analyze import signals as sig
from clipper.models import Segment, Transcript, Word


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
