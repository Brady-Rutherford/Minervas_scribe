"""Single self-contained interactive transcript (inline CSS/JS, no external assets).

Chapters from Forum timeline segments, speaker filter, search, timestamps (anchor links),
chat messages inline, low-confidence words underlined, Unattributed greyed. All text is
escaped server-side, so the page renders (and is searchable) even without JavaScript.
"""

from __future__ import annotations

import html
from pathlib import Path

from ..schema import speaker_label
from ..timeutil import fmt_clock

PALETTE = ["#751014", "#1f6f8b", "#b85c00", "#3c7a3b", "#6b4c9a", "#a23b72", "#2f5d50",
           "#8a6d00", "#3a5a98", "#9c4a2f", "#47705f", "#7d3c98"]

CSS = """
:root{--bg:#f7f5ef;--panel:#fff;--ink:#1d1b18;--muted:#6d675d;--line:#e3ded2;--accent:#751014;
--lc:#b9831a;--chip-ink:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--panel:#1d1c1a;--ink:#ece8df;--muted:#a39d91;
--line:#34312c;--accent:#e0848a;--lc:#e3b55a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:20px 24px 12px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 4px;font-size:22px;line-height:1.25}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 14px}
.warn{color:var(--accent);font-size:13px;margin-top:6px}
.wrap{display:grid;grid-template-columns:270px minmax(0,1fr);gap:0;max-width:1200px;margin:0 auto}
aside{position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;padding:16px;
border-right:1px solid var(--line)}
aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
margin:18px 0 6px}
aside input[type=search]{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--panel);color:var(--ink);font:inherit}
.count{font-size:12px;color:var(--muted);margin-top:6px}
.sp{display:flex;align-items:center;gap:6px;font-size:13px;padding:2px 0;cursor:pointer}
.sp small{color:var(--muted);margin-left:auto}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.chap a{display:block;color:var(--ink);text-decoration:none;font-size:13px;padding:2px 0}
.chap a span{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:6px}
main{padding:8px 24px 80px;min-width:0}
.ch{margin:22px 0 6px;font-size:13px;font-weight:700;color:var(--accent);
text-transform:uppercase;letter-spacing:.05em;border-top:1px solid var(--line);padding-top:10px}
.u{display:grid;grid-template-columns:58px minmax(0,1fr);gap:10px;padding:7px 0;
border-bottom:1px dashed var(--line)}
.u .ts{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px;text-decoration:none;
padding-top:3px}
.u .who{display:inline-block;font-size:12px;font-weight:700;color:var(--chip-ink);
border-radius:999px;padding:0 8px;margin-right:6px}
.u.unatt .who{background:#8f8a80!important}.u.unatt .tx{color:var(--muted);font-style:italic}
.u.me .who{outline:2px solid var(--lc)}
.lc{text-decoration:underline dotted var(--lc);text-underline-offset:3px}
.ev{font-size:12px;color:var(--muted);padding:4px 0 4px 68px}
.hide{display:none!important}
label.opt{display:flex;gap:6px;align-items:center;font-size:13px;margin-top:6px}
@media (max-width:760px){.wrap{grid-template-columns:1fr}aside{position:static;max-height:none;
border-right:0;border-bottom:1px solid var(--line)}main{padding:8px 16px 60px}}
"""

JS = """
(function(){
  var q=document.getElementById('q'), cnt=document.getElementById('cnt');
  var rows=[].slice.call(document.querySelectorAll('.u'));
  var evs=[].slice.call(document.querySelectorAll('.ev'));
  var boxes=[].slice.call(document.querySelectorAll('input[data-sp]'));
  var chat=document.getElementById('chat'), mine=document.getElementById('mine');
  function apply(){
    var term=(q.value||'').trim().toLowerCase(), on={}, shown=0;
    boxes.forEach(function(b){on[b.getAttribute('data-sp')]=b.checked;});
    rows.forEach(function(r){
      var ok=on[r.getAttribute('data-sp')]!==false;
      if(ok&&mine&&mine.checked) ok=r.classList.contains('me');
      if(ok&&term) ok=r.textContent.toLowerCase().indexOf(term)>=0;
      r.classList.toggle('hide',!ok); if(ok) shown++;
    });
    evs.forEach(function(e){e.classList.toggle('hide',!(chat&&chat.checked));});
    cnt.textContent=shown+' of '+rows.length+' utterances';
  }
  [q,chat,mine].forEach(function(el){ if(el) el.addEventListener('input',apply); });
  boxes.forEach(function(b){b.addEventListener('change',apply);});
  apply();
})();
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _text_with_spans(text: str, spans: list) -> str:
    out, pos = [], 0
    for a, b in sorted(spans or []):
        if a < pos:
            continue
        out.append(_e(text[pos:a]))
        out.append(f'<span class="lc" title="low confidence">{_e(text[a:b])}</span>')
        pos = b
    out.append(_e(text[pos:]))
    return "".join(out)


def render_html(t: dict) -> str:
    c = t["class"]
    speakers = t.get("speakers", {})
    color = {sid: PALETTE[i % len(PALETTE)] for i, sid in enumerate(speakers)}
    asr = t.get("asr") or {}

    meta = [x for x in (c.get("course_code"), c.get("term"), c.get("date")) if x]
    meta += [f"Class {c['id']}", t["alignment"]["timing"],
             f"ASR: {asr.get('provider')} {asr.get('model')}", f"privacy: {t['privacy']}"]
    head = [f"<h1>{_e(c['title'])}</h1>",
            '<div class="meta">' + "".join(f"<span>{_e(m)}</span>" for m in meta) + "</div>"]
    for w in t.get("warnings") or []:
        head.append(f'<div class="warn">{_e(w)}</div>')

    side = ['<input type="search" id="q" placeholder="Search transcript" autocomplete="off">',
            '<div class="count" id="cnt"></div>',
            '<label class="opt"><input type="checkbox" id="mine"> Only my turns</label>',
            '<label class="opt"><input type="checkbox" id="chat"> Show chat</label>',
            "<h2>Speakers</h2>"]
    has_unatt = any(u["speaker_id"] is None for u in t["utterances"])
    for sid, sp in speakers.items():
        side.append(
            f'<label class="sp"><input type="checkbox" checked data-sp="{_e(sid)}">'
            f'<span class="dot" style="background:{color[sid]}"></span>'
            f'{_e(speaker_label(t, sid))}{" (me)" if sp.get("is_me") else ""}'
            f'<small>{_e(fmt_clock(sp.get("transcript_s")))}</small></label>')
    if has_unatt:
        side.append('<label class="sp"><input type="checkbox" checked data-sp="">'
                    '<span class="dot" style="background:#8f8a80"></span>Unattributed</label>')
    if t.get("timeline"):
        side.append("<h2>Chapters</h2><div class=\"chap\">")
        for i, ch in enumerate(t["timeline"]):
            side.append(f'<a href="#ch-{i}"><span>{_e(fmt_clock(ch["t"]))}</span>'
                        f'{_e(ch["title"] or ch.get("section") or "Chapter")}</a>')
        side.append("</div>")

    body = []
    chapters = list(t.get("timeline") or [])
    events = [e for e in t.get("events") or [] if e.get("t") is not None and e.get("text")]
    ci = ei = 0
    for u in t["utterances"]:
        while ci < len(chapters) and chapters[ci]["t"] <= u["start"]:
            ch = chapters[ci]
            body.append(f'<div class="ch" id="ch-{ci}">{_e(fmt_clock(ch["t"]))} · '
                        f'{_e(ch["title"] or ch.get("section") or "Chapter")}</div>')
            ci += 1
        while ei < len(events) and events[ei]["t"] <= u["start"]:
            ev = events[ei]
            who = speaker_label(t, ev.get("speaker_id")) if ev.get("speaker_id") else "?"
            body.append(f'<div class="ev hide">[{_e(fmt_clock(ev["t"]))}] {_e(ev["type"])} · '
                        f'{_e(who)}: {_e(ev["text"])}</div>')
            ei += 1
        if not u["text"]:
            continue
        sid = u["speaker_id"]
        sp = speakers.get(sid) or {}
        cls = "u" + (" unatt" if sid is None else "") + (" me" if sp.get("is_me") else "")
        anchor = f"t-{int(u['start'])}"
        conf = "" if u["confidence"] is None else f"confidence {u['confidence']:.2f}; "
        how = ", ".join(f"{k} {v:.0%}" for k, v in (u.get("attribution") or {}).items())
        body.append(
            f'<div class="{cls}" id="{anchor}" data-sp="{_e(sid or "")}">'
            f'<a class="ts" href="#{anchor}">{_e(fmt_clock(u["start"]))}</a><div>'
            f'<span class="who" style="background:{color.get(sid, "#8f8a80")}" '
            f'title="{_e(conf + "attribution: " + how)}">{_e(speaker_label(t, sid))}</span>'
            f'<span class="tx">{_text_with_spans(u["text"], u.get("low_conf"))}</span>'
            "</div></div>")

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_e(c['title'])}</title><style>{CSS}</style></head><body>"
        f"<header>{''.join(head)}</header><div class=\"wrap\"><aside>{''.join(side)}</aside>"
        f"<main>{''.join(body)}</main></div><script>{JS}</script></body></html>"
    )


def write_html(transcript: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(render_html(transcript), encoding="utf-8")
    return path
