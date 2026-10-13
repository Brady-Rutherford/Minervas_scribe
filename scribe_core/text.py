"""Text helpers: spacing cleanup, token joining, fillers, edit distance."""

from __future__ import annotations

import re

# Deepgram emits these when filler_words=true. Readable exports drop them; JSONL keeps them.
FILLERS = frozenset(
    {"uh", "um", "uhm", "erm", "er", "ah", "mhm", "mm", "hmm", "mm-hmm", "uh-huh", "uh-uh",
     "mm-mm", "nuh-uh"}
)

_PUNCT_ONLY = re.compile(r"^[\.,!?;:%\)\]\}…]+$")


def normalize_spacing(text: str) -> str:
    """Port of v2 ``normalize_sentence_spacing`` (logic.py) for the v3 engine."""
    if not text:
        return text
    text = re.sub(r"[​-‍﻿]", "", text)
    text = text.replace(" ", " ")
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"(\.\.\.)(?=\S)", r"\1 ", text)
    text = re.sub(r"(?<!\.)([.!?])(?=([\"“'(\[]?[A-Za-z]))", r"\1 ", text)
    text = re.sub(r"([:;])(?=([\"“'(\[]?[A-Za-z]))", r"\1 ", text)
    text = re.sub(r"([.!?][\"”')\]])(?=\S)", r"\1 ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def bare(token: str) -> str:
    """Lower-case token with surrounding punctuation removed (for filler / name matching)."""
    return re.sub(r"^[^\w'-]+|[^\w'-]+$", "", token or "").lower()


def is_filler(token: str) -> bool:
    return bare(token) in FILLERS


def join_with_spans(tokens: list[str], flags: list[bool] | None = None
                    ) -> tuple[str, list[list[int]]]:
    """Join word tokens with single spaces (bare punctuation attaches to the previous token).

    Returns the text and the [start, end) character spans of tokens whose flag is True.
    """
    text = ""
    spans: list[list[int]] = []
    for i, tok in enumerate(tokens):
        t = (tok or "").strip()
        if not t:
            continue
        if text and _PUNCT_ONLY.match(t):
            start = len(text)
            text += t
        else:
            if text:
                text += " "
            start = len(text)
            text += t
        if flags is not None and flags[i]:
            spans.append([start, start + len(t)])
    return text, spans


def join_tokens(tokens: list[str]) -> str:
    return join_with_spans(tokens)[0]


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def transfer_punctuation(words: list[dict], segments: list[dict], eps: float = 0.05) -> None:
    """Whisper word timestamps come without punctuation while segment text has it.

    When a segment's whitespace tokens line up 1:1 with the words inside its time span,
    copy the punctuated tokens onto the words (in place). Otherwise leave words alone.
    """
    if not words or not segments:
        return
    i = 0
    for seg in segments:
        s0, s1 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        tokens = str(seg.get("text", "")).split()
        while i < len(words) and words[i]["start"] < s0 - eps:
            i += 1
        j = i
        while j < len(words) and words[j]["end"] <= s1 + eps:
            j += 1
        span = words[i:j]
        if span and len(span) == len(tokens) and all(
            bare(w["text"]) == bare(t) for w, t in zip(span, tokens, strict=True)
        ):
            for w, t in zip(span, tokens, strict=True):
                w["text"] = t
        i = j
