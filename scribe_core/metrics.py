"""Measuring it, so "better" is a number (plan 1.7).

Gold data = hand-labelled turns ``[{"start", "end", "user_id"}]`` (+ optional exact words for
a few minutes). ``compare_configs`` runs the three configurations from the plan on the same
class so the README table can be filled in:

    v2 baseline     old Scribe: speaker at each segment's START second, voice events < 1 s
                    dropped, no calibration, unmatched -> fallback (v2 used "Professor")
    deepgram-only   Deepgram clusters mapped to users (Hungarian), no calibration, no Forum
                    per-word evidence
    full            calibration + fusion + smoothing (the v3 pipeline)
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable

import numpy as np

from .calibrate import calibrate, shift
from .fuse import cluster_to_user, fuse, how_mix, smooth
from .text import levenshtein
from .turns import build_turns


def _who(x: dict):
    return x.get("user_id", x.get("speaker_id"))


def attribution_accuracy(pred: list[dict], gold_turns: list[dict]) -> float:
    """Seconds credited to the right person / total gold speech seconds.

    ``pred`` can be words or turns (anything with start/end/user_id or speaker_id).
    """
    total = sum(max(0.0, g["end"] - g["start"]) for g in gold_turns)
    if not total:
        return 0.0
    items = sorted(pred, key=lambda p: p["start"])
    starts = [p["start"] for p in items]
    max_len = max((p["end"] - p["start"] for p in items), default=0.0)
    correct = 0.0
    for g in gold_turns:
        lo = bisect.bisect_left(starts, g["start"] - max_len)
        hi = bisect.bisect_left(starts, g["end"])
        for p in items[lo:hi]:
            ov = min(g["end"], p["end"]) - max(g["start"], p["start"])
            if ov > 0 and _who(p) is not None and str(_who(p)) == str(_who(g)):
                correct += ov
    return min(1.0, correct / total)


def boundary_error(pred_turns: list[dict], gold_turns: list[dict]) -> float | None:
    """Median distance (s) between each gold turn start and the nearest predicted start."""
    ps = sorted(t["start"] for t in pred_turns)
    if not ps or not gold_turns:
        return None
    errs = []
    for g in gold_turns:
        k = bisect.bisect_left(ps, g["start"])
        near = [abs(g["start"] - ps[j]) for j in (k - 1, k) if 0 <= j < len(ps)]
        errs.append(min(near))
    return float(np.median(errs))


_TOKEN = re.compile(r"[\w']+")


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate (case/punctuation-insensitive)."""
    ref = _TOKEN.findall(reference.lower())
    hyp = _TOKEN.findall(hypothesis.lower())
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein(ref, hyp) / len(ref)  # works on token lists as well as strings


def name_accuracy(spoken_names: Iterable[str], transcript_text: str) -> float:
    """Share of names (as actually spoken, from gold) that appear spelled right."""
    names = [n for n in spoken_names if n]
    if not names:
        return 1.0
    low = transcript_text.lower()
    return sum(1 for n in names if n.lower() in low) / len(names)


__all__ = ["attribution_accuracy", "boundary_error", "wer", "name_accuracy", "how_mix",
           "levenshtein", "segments_from_words", "legacy_attribution", "compare_configs",
           "format_table"]


# --------------------------------------------------------------------------- 3-config runner


def segments_from_words(words: list[dict], *, max_len: float = 12.0, pause: float = 0.8
                        ) -> list[dict]:
    """Whisper-like segments (5-15 s chunks) from words, for emulating v2."""
    segs: list[dict] = []
    for w in sorted(words, key=lambda x: x["start"]):
        fits = segs and w["start"] - segs[-1]["end"] <= pause
        if fits and w["end"] - segs[-1]["start"] <= max_len:
            segs[-1]["end"] = w["end"]
            segs[-1]["text"] += " " + w["text"]
        else:
            segs.append({"start": w["start"], "end": w["end"], "text": w["text"]})
    return segs


def legacy_attribution(segments: list[dict], voice: list[dict], fallback_user=None
                       ) -> list[dict]:
    """v2 behaviour: voice events < 1 s dropped; speaker = whichever event contains the
    segment's start second; otherwise ``fallback_user`` (v2 printed "Professor")."""
    kept = [v for v in voice if v["end"] - v["start"] >= 1.0]
    out = []
    for s in segments:
        uid = next((v["user_id"] for v in kept if v["start"] <= s["start"] <= v["end"]),
                   fallback_user)
        out.append({"start": s["start"], "end": s["end"], "user_id": uid, "text": s["text"]})
    return out


def _merge_segments(segs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in segs:
        if out and out[-1]["user_id"] == s["user_id"]:
            out[-1]["end"] = s["end"]
        else:
            out.append(dict(s))
    return out


def compare_configs(words: list[dict], voice_raw: list[dict], gold_turns: list[dict], *,
                    dur: float | None = None, fallback_user=None) -> list[dict]:
    """Run the three configs on one class. ``voice_raw`` = main-room voice intervals in video
    time BEFORE calibration. Scored on turns (so pauses inside a turn count as that speaker's
    time, like the hand-labelled gold). Returns rows for ``format_table``."""
    rows = []

    legacy = legacy_attribution(segments_from_words(words), voice_raw, fallback_user)
    legacy_turns = _merge_segments(legacy)
    rows.append({"config": "v2 baseline",
                 "attribution_accuracy": attribution_accuracy(legacy_turns, gold_turns),
                 "boundary_error_s": boundary_error(legacy_turns, gold_turns),
                 "timing": "n/a"})

    mapping = cluster_to_user(words, voice_raw)
    dg = [{**w, "user_id": mapping.get(w.get("cluster")),
           "how": "cluster" if w.get("cluster") in mapping else "none"} for w in words]
    dg_turns = build_turns(dg)
    rows.append({"config": "deepgram-only",
                 "attribution_accuracy": attribution_accuracy(dg_turns, gold_turns),
                 "boundary_error_s": boundary_error(dg_turns, gold_turns),
                 "timing": "n/a"})

    cal = calibrate(words, voice_raw, dur)
    voice = shift(voice_raw, cal)
    fused = smooth(fuse(words, voice, cluster_to_user(words, voice)))
    turns = build_turns(fused)
    rows.append({"config": "full (calibrate+fuse)",
                 "attribution_accuracy": attribution_accuracy(turns, gold_turns),
                 "boundary_error_s": boundary_error(turns, gold_turns),
                 "timing": f"{cal['status']} offset={cal['offset']:+.2f}s "
                           f"drift={cal['drift']:+.5f}",
                 "how_mix": how_mix(fused)})
    return rows


def format_table(rows: list[dict]) -> str:
    def pct(x):
        return "n/a" if x is None else f"{100 * x:.1f}%"

    def secs(x):
        return "n/a" if x is None else f"{x:.2f}s"

    lines = ["| config | attribution accuracy | boundary error | timing |",
             "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['config']} | {pct(r['attribution_accuracy'])} | "
                     f"{secs(r['boundary_error_s'])} | {r['timing']} |")
    return "\n".join(lines)
