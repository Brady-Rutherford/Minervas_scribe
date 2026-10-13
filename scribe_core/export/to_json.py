from __future__ import annotations

import json
from pathlib import Path


def write_json(transcript: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
