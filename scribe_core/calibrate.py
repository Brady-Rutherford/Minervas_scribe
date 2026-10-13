"""Clock calibration between Forum voice activity and ASR speech activity (plan 1.3).

Don't trust the two zeros (video second 0 vs. Forum ``recording-started``): measure the gap.
Both sources become a 10 ms "someone is talking" signal; we find the lag that lines them up
best in 5-minute windows, then fit ``lag(t) = offset + drift * t`` through the windows.

Convention: the returned lag is what you ADD to Forum video-times to land on the audio:
``audio_t = forum_t + offset + drift * forum_t``.

Status values:
* ``ok``         one line fits every window
* ``piecewise``  a jump > ``jump_s`` mid-class (e.g. an uncaught recording restart): one line
                 per side of the largest jump
* ``unverified`` too little signal / score below ``min_score`` / shift made agreement worse:
                 NO shift is applied and the transcript says "timing unverified"
"""

from __future__ import annotations

import numpy as np

HZ = 100  # 10 ms bins


def activity(intervals, dur: float, hz: int = HZ) -> np.ndarray:
    """Binary activity signal (float32) of length dur*hz+1 from (start, end) pairs."""
    n = int(dur * hz) + 1
    diff = np.zeros(n + 1, dtype=np.int32)
    for s, e in intervals:
        a = int(max(0.0, s) * hz)
        b = int(min(dur, e) * hz)
        if b > a:
            diff[a] += 1
            diff[b] -= 1
    return (np.cumsum(diff[:n]) > 0).astype(np.float32)


def best_lag(asr_act, forum_act, max_lag_s: float = 30.0, hz: int = HZ) -> tuple[float, float]:
    """Lag (s) to ADD to Forum times so they match the audio, and its normalised peak score.

    score(k) = sum_t a[t] * f[t - k] (mean-removed), computed for all |k| <= max_lag via FFT,
    divided by |a||f| so it reads like a correlation in [-1, 1].
    """
    a = np.asarray(asr_act, dtype=np.float64)
    f = np.asarray(forum_act, dtype=np.float64)
    n = min(len(a), len(f))
    if n < 2:
        return 0.0, 0.0
    a = a[:n] - a[:n].mean()
    f = f[:n] - f[:n].mean()
    norm = float(np.linalg.norm(a) * np.linalg.norm(f))
    if norm == 0.0:
        return 0.0, 0.0
    lag_bins = min(int(round(max_lag_s * hz)), n - 1)
    nfft = 1 << int(2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(f, nfft)), nfft)
    scores = np.concatenate([corr[nfft - lag_bins:], corr[: lag_bins + 1]])  # k = -L..L
    k = int(np.argmax(scores))
    return (k - lag_bins) / hz, float(scores[k] / norm)


def _corr0(a: np.ndarray, f: np.ndarray) -> float:
    a = a - a.mean()
    f = f - f.mean()
    norm = float(np.linalg.norm(a) * np.linalg.norm(f))
    return float(np.dot(a, f) / norm) if norm else 0.0


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w)
    return float(v[np.searchsorted(cum, cum[-1] / 2.0)])


def _robust_line(t, lag, w, max_drift):
    """Weighted line fit with one round of outlier rejection; drift sanity check."""
    notes = []
    keep = np.ones(len(t), dtype=bool)
    if len(t) >= 2 and np.ptp(t) > 0:
        drift, offset = np.polyfit(t, lag, 1, w=w)
        resid = np.abs(lag - (offset + drift * t))
        mad = float(np.median(resid)) * 1.4826
        keep = resid <= max(0.5, 3.0 * mad)
        if keep.sum() >= 3 and not keep.all():
            notes.append(f"dropped {int((~keep).sum())} outlier window(s)")
            drift, offset = np.polyfit(t[keep], lag[keep], 1, w=w[keep])
    else:
        drift, offset = 0.0, float(np.average(lag, weights=w))
    if abs(drift) > max_drift:
        notes.append(f"drift {drift:.5f} s/s implausible; using a constant offset")
        drift, offset = 0.0, _weighted_median(lag[keep], w[keep])
    return float(offset), float(drift), keep, notes


def _identity(status: str, notes: list[str], **extra) -> dict:
    return {
        "status": status,
        "applied": False,
        "offset": 0.0,
        "drift": 0.0,
        "pieces": [{"t_from": None, "offset": 0.0, "drift": 0.0}],
        "notes": notes,
        **extra,
    }


def calibrate(
    words,
    voice,
    dur: float | None = None,
    *,
    win_s: float = 300.0,
    step_s: float = 150.0,
    max_lag_s: float = 30.0,
    min_window_score: float = 0.3,
    min_score: float = 0.3,
    max_drift: float = 0.005,
    jump_s: float = 1.5,
    hz: int = HZ,
) -> dict:
    """Measure offset (+ drift) between Forum voice intervals and ASR words (both video-time)."""
    w_iv = [(float(w["start"]), float(w["end"])) for w in words]
    v_iv = [(float(v["start"]), float(v["end"])) for v in voice]
    if not w_iv or not v_iv:
        reason = "no ASR words" if not w_iv else "no Forum voice events in the main room"
        return _identity("unverified", [reason], score=0.0, windows=[])
    if dur is None:
        dur = max(max(e for _, e in w_iv), max(e for _, e in v_iv))
    asr = activity(w_iv, dur, hz)
    frm = activity(v_iv, dur, hz)
    score_before = _corr0(asr.astype(np.float64), frm.astype(np.float64))

    pts = []
    t0 = 0.0
    while True:
        i, j = int(t0 * hz), int(min(dur, t0 + win_s) * hz)
        if j - i > hz and asr[i:j].any() and frm[i:j].any():
            lag, sc = best_lag(asr[i:j], frm[i:j], max_lag_s, hz)
            if sc >= min_window_score:
                pts.append((t0 + (j - i) / hz / 2.0, lag, sc))
        if t0 + win_s >= dur:
            break
        t0 += step_s

    notes: list[str] = []
    if len(pts) < 3:
        lag, sc = best_lag(asr, frm, max_lag_s, hz)
        notes.append(f"only {len(pts)} usable window(s); whole-class fit, no drift")
        offset, drift, score, method, max_res = lag, 0.0, sc, "global", 0.0
        pieces = [{"t_from": None, "offset": offset, "drift": 0.0}]
        status = "ok"
    else:
        t, lag, w = (np.array(x, dtype=np.float64) for x in zip(*pts, strict=True))
        offset, drift, keep, n1 = _robust_line(t, lag, w, max_drift)
        notes += n1
        resid = np.abs(lag - (offset + drift * t))
        max_res = float(resid[keep].max()) if keep.any() else 0.0
        score, method, status = float(np.average(w)), "windowed", "ok"
        pieces = [{"t_from": None, "offset": offset, "drift": drift}]
        if max_res > jump_s:
            jumps = np.abs(np.diff(lag))
            k = int(np.argmax(jumps))
            left, right = slice(0, k + 1), slice(k + 1, None)
            o1, d1, _, n2 = _robust_line(t[left], lag[left], w[left], max_drift)
            o2, d2, _, n3 = _robust_line(t[right], lag[right], w[right], max_drift)
            boundary = float((t[k] + t[k + 1]) / 2.0)
            pieces = [{"t_from": None, "offset": o1, "drift": d1},
                      {"t_from": boundary, "offset": o2, "drift": d2}]
            notes += n2 + n3
            notes.append(f"jump of {jumps[k]:.2f}s near t={boundary:.0f}s; fitted two pieces")
            fit = np.where(t < boundary, o1 + d1 * t, o2 + d2 * t)
            max_res = float(np.abs(lag - fit).max())
            status = "piecewise"

    windows = [[round(a, 2), round(b, 3), round(c, 3)] for a, b, c in pts]
    if score < min_score:
        return _identity("unverified", notes + [f"score {score:.2f} < {min_score}"],
                         score=score, windows=windows, score_before=score_before)
    cal = {
        "status": status,
        "applied": True,
        "offset": float(pieces[0]["offset"]),
        "drift": float(pieces[0]["drift"]),
        "pieces": pieces,
        "score": float(score),
        "score_before": score_before,
        "max_residual_s": float(max_res),
        "method": method,
        "windows": windows,
        "notes": notes,
    }
    shifted = activity([(fn_t, fn_e) for fn_t, fn_e in _shift_pairs(v_iv, cal)], dur, hz)
    cal["score_after"] = _corr0(asr.astype(np.float64), shifted.astype(np.float64))
    if cal["score_after"] + 0.01 < score_before:
        return _identity("unverified", notes + ["shift made agreement worse; not applied"],
                         score=score, windows=windows, score_before=score_before,
                         score_after=cal["score_after"])
    return cal


def make_shift(cal: dict):
    """Forum video-time -> audio time, following the (possibly piecewise) calibration."""
    pieces = cal.get("pieces") or [{"t_from": None, "offset": 0.0, "drift": 0.0}]
    if not cal.get("applied", True):
        pieces = [{"t_from": None, "offset": 0.0, "drift": 0.0}]
    bounds = [p["t_from"] for p in pieces[1:]]

    def f(t: float) -> float:
        idx = 0
        for b in bounds:
            if t >= b:
                idx += 1
            else:
                break
        p = pieces[idx]
        return t + p["offset"] + p["drift"] * t

    return f


def _shift_pairs(pairs, cal):
    f = make_shift(cal)
    return [(f(s), f(e)) for s, e in pairs]


def shift(voice: list[dict], cal: dict) -> list[dict]:
    """Calibrated copy of the voice intervals (identity when calibration wasn't applied)."""
    f = make_shift(cal)
    return [{**v, "start": f(v["start"]), "end": f(v["end"])} for v in voice]


def timing_label(cal: dict) -> str:
    if not cal.get("applied"):
        return "timing unverified"
    return f"timing check: {cal.get('score', 0.0):.2f}"
