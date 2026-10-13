"""Markdown (Obsidian-friendly): header, chapter list, then the transcript by chapter."""

from __future__ import annotations

from pathlib import Path

from ..schema import speaker_label
from ..timeutil import fmt_clock


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def render_md(t: dict) -> str:
    c = t["class"]
    lines = [f"# {_esc(c['title'])}", ""]
    meta = [x for x in (c.get("course_code"), c.get("term"), c.get("date")) if x]
    asr = t.get("asr") or {}
    lines.append(" · ".join(_esc(str(x)) for x in meta) or "")
    lines.append("")
    lines.append(f"- Class ID: {c['id']}")
    lines.append(f"- {t['alignment']['timing']}")
    lines.append(f"- ASR: {asr.get('provider')} {asr.get('model')} ({asr.get('mode')})")
    lines.append(f"- Privacy: {t['privacy']}")
    for w in t.get("warnings") or []:
        lines.append(f"- Warning: {_esc(w)}")
    lines.append("")
    if t.get("timeline"):
        lines.append("## Chapters")
        lines.append("")
        for ch in t["timeline"]:
            lines.append(f"- [{fmt_clock(ch['t'])}] {_esc(ch['title'] or ch.get('section') or '')}")
        lines.append("")
    lines.append("## Transcript")
    lines.append("")
    chapters = list(t.get("timeline") or [])
    ci = 0
    for u in t["utterances"]:
        while ci < len(chapters) and chapters[ci]["t"] <= u["start"]:
            title = chapters[ci]["title"] or chapters[ci].get("section") or "Chapter"
            lines.append(f"### [{fmt_clock(chapters[ci]['t'])}] {_esc(title)}")
            lines.append("")
            ci += 1
        if not u["text"]:
            continue
        who = _esc(speaker_label(t, u["speaker_id"]))
        lines.append(f"**[{fmt_clock(u['start'])}] {who}:** {_esc(u['text'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_md(transcript: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(render_md(transcript), encoding="utf-8")
    return path
