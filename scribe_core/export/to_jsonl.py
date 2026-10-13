"""JSONL dataset export: one row per utterance (feeds the CS156 "Me, as a model" work).

``text`` keeps filler words (an "um" before a contribution is signal); ``text_clean`` drops
them. ``timeline_segment`` is the Forum chapter active when the utterance starts.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..schema import chapter_at


def jsonl_rows(transcript: dict) -> list[dict]:
    cid = transcript["class"]["id"]
    speakers = transcript.get("speakers", {})
    rows = []
    for u in transcript["utterances"]:
        sp = speakers.get(u["speaker_id"]) or {}
        rows.append({
            "class_id": cid,
            "start": u["start"],
            "end": u["end"],
            "speaker_id": u["speaker_id"],
            "speaker_role": sp.get("role"),
            "is_me": bool(sp.get("is_me")),
            "text": u["text_verbatim"],
            "text_clean": u["text"],
            "timeline_segment": chapter_at(transcript, u["start"]),
            "confidence": u["confidence"],
        })
    return rows


def write_jsonl(transcript: dict, path: Path) -> Path:
    path = Path(path)
    with open(path, "w", encoding="utf-8") as fh:
        for row in jsonl_rows(transcript):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path
