"""Plan 1.6 (partial): shaky-span detection + constrained name/term fix-up.

* ``find_shaky_spans`` finds stretches where word confidence averages below ~0.6 for 5+ s
  (usually a quiet student or a bad mic). The Groq "second opinion" re-transcription of
  those spans is NOT implemented yet (see V3_README "not done yet"); the spans are reported
  in the transcript so it can be added without touching the rest of the pipeline.
* ``name_fixup`` only touches words under 0.6 confidence, only swaps them for an exact roster
  name / glossary token at edit distance <= 2 (<= 1 for short words), only when the best
  candidate is unique, and never touches common English words. Timings never change. It is
  opt-in (``--name-fixup``) and every change is listed in the transcript. There is no
  free-form LLM rewriting anywhere: that's how invented quotes appear.
"""

from __future__ import annotations

import re

from .text import bare, levenshtein

_COMMON = """
a about actually after again all also an and answer any are around as at back be because been
before being but by can class could data day did do does doing done down each even every
example false first for from get give go going good got great had has have he her here him his
how i idea ideas if in into is it its just kind know last like little long look made make many
may maybe me mean model models more most much must my need never new next no not now of off
okay on one only or other our out over part people point pretty problem question quite rather
read reading really right said same say see she should slide slides so some sort still such
sure take talk team than thank thanks that the their them then there these they thing things
think this those through time to today too true two under up us use very want was way we week
well were what when where which while who why will with word words work world would wrong yeah
year years yes you your
"""
COMMON_WORDS = frozenset(_COMMON.split())  # noqa: SIM905 - readable word list

_EDGE_PUNCT = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def find_shaky_spans(words: list[dict], *, threshold: float = 0.6, min_s: float = 5.0,
                     max_gap_s: float = 1.0) -> list[dict]:
    """Stretches whose mean word confidence stays below ``threshold`` for >= ``min_s``."""
    spans = []
    i, n = 0, len(words)
    while i < n:
        if words[i].get("conf") is None or words[i]["conf"] >= threshold:
            i += 1
            continue
        j, total = i, 0.0
        while j < n and words[j].get("conf") is not None:
            if j > i and words[j]["start"] - words[j - 1]["end"] > max_gap_s:
                break
            if (total + words[j]["conf"]) / (j - i + 1) >= threshold:
                break
            total += words[j]["conf"]
            j += 1
        start, end = words[i]["start"], words[j - 1]["end"]
        if end - start >= min_s:
            spans.append({"start": start, "end": end, "n_words": j - i,
                          "mean_conf": round(total / (j - i), 3)})
        i = max(j, i + 1)
    return spans


def _vocab(terms) -> dict[str, str]:
    """lower-case token -> canonical spelling, from names / glossary terms (len >= 3)."""
    vocab: dict[str, str] = {}
    for term in terms:
        for tok in str(term).split():
            core = tok.strip(".,;:!?()[]\"'")
            if len(core) >= 3 and core.lower() not in COMMON_WORDS:
                vocab.setdefault(core.lower(), core)
    return vocab


def name_fixup(words: list[dict], terms, *, max_dist: int = 2, conf: float = 0.6
               ) -> tuple[list[dict], list[dict]]:
    """Return (new_words, changes). Never alters timings; see module docstring for rules."""
    vocab = _vocab(terms)
    out, changes = [], []
    for w in words:
        c = w.get("conf")
        m = _EDGE_PUNCT.match(w.get("text") or "")
        lead, core, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", w["text"], "")
        low = bare(core)
        if (c is None or c >= conf or len(low) < 4 or low in COMMON_WORDS or low in vocab
                or not vocab):
            out.append(w)
            continue
        allowed = 1 if len(low) <= 5 else max_dist
        scored = sorted((levenshtein(low, k), k) for k in vocab)
        best = [k for d, k in scored if d == scored[0][0]]
        if scored[0][0] > allowed or len(best) != 1:
            out.append(w)
            continue
        new_text = f"{lead}{vocab[best[0]]}{trail}"
        changes.append({"start": w["start"], "from": w["text"], "to": new_text,
                        "conf": c, "distance": scored[0][0]})
        out.append({**w, "text": new_text, "fixup": True})
    return out, changes
