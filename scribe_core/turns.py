"""Turns (plan 1.5): consecutive words with the same speaker, split on change or long pause."""

from __future__ import annotations

from collections import Counter

from .text import join_with_spans

PAUSE_S = 1.2
LOW_CONF = 0.6


def build_turns(words: list[dict], *, pause: float = PAUSE_S, low_conf: float = LOW_CONF
                ) -> list[dict]:
    """Group fused words into turns.

    Each turn: start, end, user_id, text (fillers removed), text_verbatim (with fillers),
    confidence (mean word confidence), attribution (share of words by ``how``), n_words,
    low_conf (character spans in ``text`` of words under ``low_conf`` so viewers can
    underline / grey them), cluster (majority diarization cluster, for debugging).
    """
    turns: list[dict] = []
    group: list[dict] = []

    def flush():
        if group:
            turns.append(_make_turn(group, low_conf))

    for w in sorted(words, key=lambda x: (x["start"], x["end"])):
        if group and (
            w.get("user_id") != group[-1].get("user_id") or w["start"] - group[-1]["end"] > pause
        ):
            flush()
            group = []
        group.append(w)
    flush()
    return turns


def _make_turn(group: list[dict], low_conf: float) -> dict:
    clean = [w for w in group if not w.get("filler")]
    confs = [w["conf"] for w in group if w.get("conf") is not None]
    text, spans = join_with_spans(
        [w["text"] for w in clean],
        [w.get("conf") is not None and w["conf"] < low_conf for w in clean],
    )
    verbatim, _ = join_with_spans([w["text"] for w in group])
    hows = Counter(w.get("how", "none") for w in group)
    clusters = Counter(w.get("cluster") for w in group if w.get("cluster") is not None)
    return {
        "start": round(float(group[0]["start"]), 3),
        "end": round(float(max(w["end"] for w in group)), 3),
        "user_id": group[0].get("user_id"),
        "text": text,
        "text_verbatim": verbatim,
        "confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "attribution": {k: round(v / len(group), 3) for k, v in hows.most_common()},
        "n_words": len(group),
        "low_conf": spans,
        "cluster": clusters.most_common(1)[0][0] if clusters else None,
    }
