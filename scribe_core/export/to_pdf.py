"""PDF export (optional extra ``pdf`` = reportlab). Same layout as v2's ``compile_pdf``
(title, class info, attendance, Time/Speaker/Contribution table) but rendered from the v2
transcript object, with "Unattributed" instead of "Professor" for unmatched speech."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from ..schema import speaker_label
from ..timeutil import fmt_clock


def write_pdf(transcript: dict, path: Path) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("PDF export needs reportlab: pip install -e '.[pdf]'") from exc

    t = transcript
    c = t["class"]
    path = Path(path)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=styles["Normal"], fontName="Helvetica", fontSize=10,
                          leading=12, wordWrap="CJK")
    grey = ParagraphStyle("Grey", parent=body, textColor=colors.grey)
    head = ParagraphStyle("Head", parent=styles["Normal"], fontName="Helvetica-Bold",
                          fontSize=11, textColor=colors.whitesmoke, alignment=1)
    left = ParagraphStyle("Left", parent=styles["Normal"], alignment=0)

    def p(text, style=body):
        return Paragraph(escape(str(text or "")), style)

    grid = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#751014")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    el = [p(c["title"], styles["Title"])]
    if c.get("section_title"):
        el.append(p(c["section_title"], ParagraphStyle("C", parent=styles["Heading3"],
                                                       alignment=1)))
    el.append(Spacer(1, 8))
    for label, value in (("Class ID", c["id"]), ("Course", c.get("course_code")),
                         ("Date", c.get("date")), ("Recording start", c.get("recording_start")),
                         ("Timing", t["alignment"]["timing"]),
                         ("ASR", f"{t['asr'].get('provider')} {t['asr'].get('model')}"),
                         ("Privacy", t["privacy"])):
        if value:
            el.append(Paragraph(f"<b>{escape(label)}:</b> {escape(str(value))}", left))
    if c.get("url"):
        el.append(Paragraph(f'<b>Class link:</b> <a href="{escape(c["url"])}">'
                            f'{escape(c["url"])}</a>', left))
    for w in t.get("warnings") or []:
        el.append(Paragraph(f"<i>{escape(w)}</i>", left))
    el.append(Spacer(1, 12))

    if t.get("attendance"):
        el.append(p("Attendance", styles["Heading3"]))
        rows = [[p("Student", head), p("Status", head)]]
        for a in t["attendance"]:
            who = a.get("name") or a.get("speaker_id")
            rows.append([p(who), p("Absent" if a.get("absent") else "Present")])
        table = Table(rows, colWidths=[4.5 * inch, 1.5 * inch], repeatRows=1)
        table.setStyle(grid)
        el += [table, Spacer(1, 14)]

    el.append(p("Transcript", styles["Heading3"]))
    rows = [[p("Time", head), p("Speaker", head), p("Contribution", head)]]
    for u in t["utterances"]:
        if not u["text"]:
            continue
        unatt = u["speaker_id"] is None
        style = grey if unatt else body
        rows.append([p(fmt_clock(u["start"])), p(speaker_label(t, u["speaker_id"]), style),
                     p(u["text"], style)])
    table = Table(rows, colWidths=[0.75 * inch, 1.9 * inch, 4.35 * inch], repeatRows=1)
    table.setStyle(grid)
    el.append(table)

    SimpleDocTemplate(str(path), pagesize=letter, rightMargin=60, leftMargin=60, topMargin=60,
                      bottomMargin=60, title=str(c["title"])).build(el)
    return path
