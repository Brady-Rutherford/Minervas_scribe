import numpy as np
import pytest

from scribe_core.calibrate import activity, best_lag, calibrate, make_shift, shift, timing_label


def synthetic_speech(dur: float, seed: int = 7):
    """Alternating speech bursts / pauses over ``dur`` seconds (audio time)."""
    rng = np.random.default_rng(seed)
    t, out = 1.0, []
    while t < dur - 10:
        length = float(rng.uniform(0.3, 9.0))
        out.append((t, min(dur, t + length)))
        t += length + float(rng.uniform(0.3, 4.0))
    return out


def as_words(intervals):
    return [{"start": s, "end": e} for s, e in intervals]


def as_voice(intervals):
    return [{"start": s, "end": e, "user_id": "1"} for s, e in intervals]


def forum_from_audio(intervals, offset, drift, jitter=0.08, seed=3):
    """Invert audio_t = forum_t + offset + drift*forum_t, with edge jitter (mic VAD)."""
    rng = np.random.default_rng(seed)
    out = []
    for s, e in intervals:
        fs = (s - offset) / (1 + drift) + float(rng.normal(0, jitter))
        fe = (e - offset) / (1 + drift) + float(rng.normal(0, jitter))
        if fe > fs > 0:
            out.append((fs, fe))
    return out


def test_activity_signal_bins_at_10ms():
    x = activity([(0.5, 1.0)], 2.0)
    assert x.shape == (201,)
    assert x[49] == 0 and x[50] == 1 and x[99] == 1 and x[100] == 0


def test_best_lag_fft_matches_brute_force():
    rng = np.random.default_rng(0)
    f = (rng.random(800) > 0.6).astype(float)
    a = np.roll(f, 37)  # audio = forum delayed by 37 bins
    lag, score = best_lag(a, f, max_lag_s=1.0, hz=100)
    assert lag == pytest.approx(0.37)
    ac, fc = a - a.mean(), f - f.mean()
    brute = {k: sum(ac[t] * fc[t - k] for t in range(len(a)) if 0 <= t - k < len(f))
             for k in range(-100, 101)}
    assert max(brute, key=brute.get) == 37
    assert score == pytest.approx(brute[37] / (np.linalg.norm(ac) * np.linalg.norm(fc)), rel=1e-6)


def test_calibrate_recovers_known_offset():
    audio = synthetic_speech(1800)
    forum = forum_from_audio(audio, offset=2.37, drift=0.0)
    cal = calibrate(as_words(audio), as_voice(forum), 1800)
    assert cal["applied"] is True and cal["status"] == "ok"
    assert cal["offset"] == pytest.approx(2.37, abs=0.05)
    assert abs(cal["drift"]) < 1e-4
    assert cal["score"] > 0.5
    assert cal["score_after"] > cal["score_before"]
    assert timing_label(cal).startswith("timing check: ")


def test_calibrate_recovers_offset_and_drift():
    audio = synthetic_speech(5400, seed=11)
    forum = forum_from_audio(audio, offset=-1.8, drift=0.0012)  # ~6.5 s over 90 min
    cal = calibrate(as_words(audio), as_voice(forum), 5400)
    assert cal["status"] == "ok"
    assert cal["offset"] == pytest.approx(-1.8, abs=0.1)
    assert cal["drift"] == pytest.approx(0.0012, abs=1e-4)
    f = make_shift(cal)
    t = 4000.0
    assert f(t) == pytest.approx(t + (-1.8) + 0.0012 * t, abs=0.12)
    assert cal["max_residual_s"] < 0.5


def test_calibrate_flags_a_jump_as_piecewise():
    audio = synthetic_speech(3600, seed=5)
    early = [iv for iv in audio if iv[1] < 1800]
    late = [iv for iv in audio if iv[0] >= 1800]
    forum = forum_from_audio(early, 1.0, 0.0) + forum_from_audio(late, 6.0, 0.0, seed=4)
    cal = calibrate(as_words(audio), as_voice(forum), 3600)
    assert cal["status"] == "piecewise"
    assert len(cal["pieces"]) == 2
    f = make_shift(cal)
    assert f(600) - 600 == pytest.approx(1.0, abs=0.15)
    assert f(3000) - 3000 == pytest.approx(6.0, abs=0.15)


def test_unrelated_signals_are_unverified_and_not_shifted():
    audio = synthetic_speech(1200, seed=1)
    noise = synthetic_speech(1200, seed=99)
    cal = calibrate(as_words(audio), as_voice(noise), 1200)
    assert cal["applied"] is False and cal["status"] == "unverified"
    assert timing_label(cal) == "timing unverified"
    voice = as_voice(noise)
    assert shift(voice, cal) == voice


def test_no_forum_voice_is_unverified():
    cal = calibrate(as_words([(1, 2)]), [], 10)
    assert cal["status"] == "unverified" and cal["offset"] == 0.0
