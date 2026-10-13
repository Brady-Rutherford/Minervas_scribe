"""CSV: one row per utterance. ``class_id`` comes from the class object (fixes v2 bug B11,
where it was parsed out of the referer header and went blank without one)."""

from __future__ import annotations

import csv
from pathlib import Path

from ..schema import speaker_label

COLUMNS = ["class_id", "speaker_id", "speaker", "start", "end", "text", "confidence"]


def csv_rows(transcript: dict) -> list[dict]:
    cid = transcript["class"]["id"]
    rows = []
    for u in transcript["utterances"]:
        if not u["text"]:
            continue  # filler-only turn
        rows.append({
            "class_id": cid,
            "speaker_id": u["speaker_id"] or "",
            "speaker": speaker_label(transcript, u["speaker_id"]),
            "start": f"{u['start']:.3f}",
            "end": f"{u['end']:.3f}",
            "text": u["text"],
            "confidence": "" if u["confidence"] is None else f"{u['confidence']:.3f}",
        })
    return rows


def write_csv(transcript: dict, path: Path) -> Path:
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows(transcript))
    return path
