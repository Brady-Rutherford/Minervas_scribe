"""Four-lane debug timeline (plan 1.8).

Lanes: Forum voice events (raw), Forum voice events (calibrated), Deepgram clusters, final
turns + words colour-coded by ``how``. When something's wrong you can see in seconds whether
it's calibration, diarization, or mic bleed. Self-contained; zoom via a CSS variable.
Privacy: callers pass a ``label`` function that already respects names/ids mode.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import Path

from ..calibrate import timing_label
from ..timeutil import fmt_clock
from .to_html import PALETTE

HOW_COLORS = {"forum+cluster": "#2e7d32", "forum": "#66bb6a", "cluster": "#1e88e5",
              "forum-overlap": "#fb8c00", "conflict": "#e53935", "none": "#9e9e9e"}

CSS = """
:root{--pps:8;--bg:#fbfaf7;--ink:#1d1b18;--muted:#6d675d;--line:#e3ded2}
@media (prefers-color-scheme:dark){:root{--bg:#151413;--ink:#ece8df;--muted:#a39d91;
--line:#34312c}}
body{margin:0;background:var(--bg);color:var(--ink);
font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}
.muted{color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:4px;margin-right:12px}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
.tools{margin-top:8px}button{font:inherit;padding:3px 10px;margin-right:4px}
.scroll{overflow-x:auto;padding:10px 18px 40px}
.lane{position:relative;margin:10px 0 2px;font-weight:700}
.rows{position:relative;width:calc(var(--dur) * var(--pps) * 1px);
border-top:1px solid var(--line)}
.row{position:relative;height:14px;border-bottom:1px dotted var(--line)}
.row .lbl{position:sticky;left:0;z-index:2;font-size:10px;color:var(--muted);background:var(--bg);
padding:0 4px;white-space:nowrap}
.b{position:absolute;top:2px;height:10px;border-radius:2px;
left:calc(var(--s) * var(--pps) * 1px);width:max(1px,calc(var(--d) * var(--pps) * 1px))}
.ruler .tick{position:absolute;top:0;font-size:10px;color:var(--muted);
left:calc(var(--s) * var(--pps) * 1px);border-left:1px solid var(--line);padding-left:2px}
table{border-collapse:collapse;margin:8px 18px}td,th{border:1px solid var(--line);padding:2px 8px}
"""

JS = """
function zoom(f){var r=document.documentElement,
v=parseFloat(getComputedStyle(r).getPropertyValue('--pps'))||8;
r.style.setProperty('--pps',Math.max(0.25,Math.min(200,v*f)));}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _block(s: float, e: float, color: str, title: str) -> str:
    return (f'<div class="b" style="--s:{s:.2f};--d:{max(0.0, e - s):.2f};background:{color}" '
            f'title="{_e(title)}"></div>')


def _lane(title: str, rows: str) -> str:
    return f'<div class="lane">{_e(title)}</div><div class="rows">{rows}</div>'


def render_debug_html(
    *,
    title: str,
    voice_raw: list[dict],
    voice_cal: list[dict],
    words: list[dict],
    turns: list[dict],
    calibration: dict,
    mapping: dict,
    label: Callable[[object], str],
) -> str:
    ends = [x["end"] for x in voice_raw + voice_cal + words + turns]
    dur = max(ends) + 5 if ends else 60.0
    users: list = []
    for src in (turns, voice_cal):
        for x in src:
            if x.get("user_id") is not None and x["user_id"] not in users:
                users.append(x["user_id"])
    ucolor = {u: PALETTE[i % len(PALETTE)] for i, u in enumerate(users)}

    def voice_lane(voice):
        rows = []
        for u in users:
            blocks = "".join(_block(v["start"], v["end"], ucolor[u],
                                    f"{label(u)} {fmt_clock(v['start'])}") for v in voice
                             if v.get("user_id") == u)
            rows.append(f'<div class="row"><span class="lbl">{_e(label(u))}</span>{blocks}</div>')
        return "".join(rows)

    clusters = sorted({w.get("cluster") for w in words if w.get("cluster") is not None})
    cl_rows = []
    for cl in clusters:
        runs: list[list[float]] = []
        for w in words:
            if w.get("cluster") != cl:
                continue
            if runs and w["start"] - runs[-1][1] < 0.5:
                runs[-1][1] = w["end"]
            else:
                runs.append([w["start"], w["end"]])
        mapped = mapping.get(cl)
        color = ucolor.get(mapped, "#9e9e9e")
        name = f"cluster {cl} → {label(mapped) if mapped is not None else 'unmapped'}"
        blocks = "".join(_block(a, b, color, name) for a, b in runs)
        cl_rows.append(f'<div class="row"><span class="lbl">{_e(name)}</span>{blocks}</div>')
    if not clusters:
        cl_rows.append('<div class="row"><span class="lbl">no diarization (groq/local)'
                       "</span></div>")

    turn_blocks = "".join(
        _block(t["start"], t["end"], ucolor.get(t["user_id"], "#9e9e9e"),
               f"{label(t['user_id']) if t['user_id'] is not None else 'Unattributed'}: "
               f"{t['text'][:200]}") for t in turns)
    word_blocks = "".join(
        _block(w["start"], w["end"], HOW_COLORS.get(w.get("how"), "#9e9e9e"),
               f"{w['text']} [{w.get('how')}{', smoothed' if w.get('smoothed') else ''}]")
        for w in words)
    ticks = "".join(f'<span class="tick" style="--s:{s}">{fmt_clock(s)}</span>'
                    for s in range(0, int(dur), 60))

    legend = "".join(f'<span><i class="sw" style="background:{c}"></i>{_e(k)}</span>'
                     for k, c in HOW_COLORS.items())
    win_rows = "".join(f"<tr><td>{fmt_clock(t)}</td><td>{lag:+.2f}s</td><td>{sc:.2f}</td></tr>"
                       for t, lag, sc in calibration.get("windows") or [])
    cal = calibration
    cal_txt = (f"{timing_label(cal)} · status {cal.get('status')} · offset "
               f"{cal.get('offset', 0):+.3f}s · drift {cal.get('drift', 0):+.6f} s/s"
               f" · max residual {cal.get('max_residual_s', 0) or 0:.2f}s")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Debug timeline</title><style>{CSS}</style></head><body>"
        f"<header><h1>{_e(title)}: debug timeline</h1><div class=\"muted\">{_e(cal_txt)}</div>"
        f"<div class=\"legend\">words by how: {legend}</div>"
        "<div class=\"tools\"><button onclick=\"zoom(1.5)\">zoom in</button>"
        "<button onclick=\"zoom(1/1.5)\">zoom out</button></div></header>"
        f"<div class=\"scroll\" style=\"--dur:{dur:.1f}\">"
        f"<div class=\"rows ruler\" style=\"height:14px\">{ticks}</div>"
        + _lane("1. Forum voice events (raw)", voice_lane(voice_raw))
        + _lane("2. Forum voice events (calibrated)", voice_lane(voice_cal))
        + _lane("3. Deepgram clusters", "".join(cl_rows))
        + _lane("4. Final turns + words by how",
                f'<div class="row"><span class="lbl">turns</span>{turn_blocks}</div>'
                f'<div class="row"><span class="lbl">words</span>{word_blocks}</div>')
        + "</div>"
        "<h3 style=\"margin:8px 18px\">Calibration windows</h3>"
        f"<table><tr><th>window centre</th><th>lag</th><th>score</th></tr>{win_rows}</table>"
        f"<script>{JS}</script></body></html>"
    )


def write_debug_html(path: Path, **kwargs) -> Path:
    path = Path(path)
    path.write_text(render_debug_html(**kwargs), encoding="utf-8")
    return path
