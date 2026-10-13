"""Exporters. Every format renders from one ``scribe.transcript/v2`` object."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .to_csv import write_csv
from .to_html import write_html
from .to_json import write_json
from .to_jsonl import write_jsonl
from .to_md import write_md
from .to_pdf import write_pdf

EXPORTERS: dict[str, Callable[[dict, Path], Path]] = {
    "json": write_json,
    "csv": write_csv,
    "md": write_md,
    "html": write_html,
    "jsonl": write_jsonl,
    "pdf": write_pdf,
}
DEFAULT_FORMATS = ("json", "csv", "md", "html", "jsonl")


def parse_formats(value: str | list[str] | tuple | None) -> list[str]:
    if value is None:
        return list(DEFAULT_FORMATS)
    items = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for item in items:
        fmt = item.strip().lower().lstrip(".")
        if not fmt:
            continue
        if fmt not in EXPORTERS:
            raise ValueError(f"Unknown format {fmt!r}; choose from {sorted(EXPORTERS)}")
        if fmt not in out:
            out.append(fmt)
    return out


def write_exports(transcript: dict, out_dir: Path, basename: str, formats,
                  skipped: list[str] | None = None) -> list[Path]:
    """Write each format. A format whose optional dependency is missing (PDF without
    reportlab) is skipped and reported in ``skipped`` instead of failing the whole job."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in parse_formats(formats):
        try:
            paths.append(EXPORTERS[f](transcript, out_dir / f"{basename}.{f}"))
        except RuntimeError as exc:
            if skipped is None:
                raise
            skipped.append(f"{f} skipped: {exc}")
    return paths
