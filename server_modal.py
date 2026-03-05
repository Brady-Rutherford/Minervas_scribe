"""
server_modal.py — Modal serverless backend for Minerva's Scribe.

Deploy:  modal deploy server_modal.py
Test:    modal serve server_modal.py   (local dev with hot reload)

After deploy, Modal prints endpoint URLs. Use those in the extension's
Backend URL field.
"""

import json
import uuid
import base64
import re
import io
import os
import csv
import subprocess
from pathlib import Path
from datetime import timedelta

import modal

app = modal.App("minervas-scribe")

volume = modal.Volume.from_name("scribe-outputs", create_if_missing=True)
job_status = modal.Dict.from_name("scribe-jobs", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "openai-whisper==20231117",
        "pydub==0.25.1",
        "requests==2.31.0",
        "iso8601==2.1.0",
        "reportlab==4.2.2",
        "tqdm==4.66.4",
        "numpy==1.26.4",
        "torch>=2.1.0",
    )
)

# ------------------------------------------------------------------ #
#  Shared helpers (subset of logic.py, inlined to run inside Modal)   #
# ------------------------------------------------------------------ #

def normalize_sentence_spacing(text):
    if not text:
        return text
    text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)
    text = text.replace('\u00A0', ' ')
    text = re.sub(r'\s*\n+\s*', ' ', text)
    text = re.sub(r'(\.\.\.)(?=\S)', r'\1 ', text)
    text = re.sub(r'(?<!\.)([.!?])(?=([""\'(\[]?[A-Za-z]))', r'\1 ', text)
    text = re.sub(r'([:;])(?=([""\'(\[]?[A-Za-z]))', r'\1 ', text)
    text = re.sub(r'([.!?][""\')\]])(?=\S)', r'\1 ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def _fmt_mmss(seconds_float):
    if seconds_float is None:
        return ""
    seconds = max(0, int(seconds_float))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

def _fmt_dt_hm(dt_str):
    if not dt_str:
        return ""
    try:
        import iso8601 as iso
        dt = iso.parse_date(dt_str)
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return dt_str.split("T")[0] if "T" in str(dt_str) else ""


def parse_forum_data(class_id, class_json, events_list):
    import iso8601 as iso
    data = class_json
    session_title = data.get("title") or f"Session {class_id}"
    course_obj = (data.get("section") or {}).get("course") or {}
    rec = (data.get("recording-sessions") or [{}])[0]
    recording_start = rec.get("recording-started")

    class_meta = {
        "session_title":   session_title,
        "course_code":     course_obj.get("course-code", ""),
        "course_title":    course_obj.get("title", ""),
        "section_title":   (data.get("section") or {}).get("title", ""),
        "schedule":        "",
        "class_type":      data.get("type", ""),
        "recording_start": recording_start,
        "recording_end":   rec.get("recording-ended"),
    }

    st = class_meta["section_title"]
    if isinstance(st, str) and "," in st:
        parts = [p.strip() for p in st.split(",", 1)]
        class_meta["schedule"] = parts[1] if len(parts) > 1 else ""

    ref_time = iso.parse_date(recording_start) if recording_start else None
    voice_events, timeline_segments = [], []
    events = events_list if isinstance(events_list, list) else []

    for ev in events:
        et = ev.get("event-type")
        if et == "voice" and ref_time:
            dur_ms = (ev.get("event-data") or {}).get("duration", 0)
            dur = dur_ms / 1000.0
            if dur >= 1:
                stt = iso.parse_date(ev["start-time"])
                ett = iso.parse_date(ev["end-time"])
                voice_events.append({
                    "start": (stt - ref_time).total_seconds(),
                    "end": (ett - ref_time).total_seconds(),
                    "duration": dur,
                    "speaker": {
                        "id": (ev.get("actor") or {}).get("id") or (ev.get("actor") or {}).get("user-id"),
                        "first-name": (ev.get("actor") or {}).get("first-name"),
                        "last-name": (ev.get("actor") or {}).get("last-name"),
                    },
                })
        elif et == "timeline-segment" and ref_time:
            stt = iso.parse_date(ev["start-time"])
            seg = ev.get("event-data") or {}
            timeline_segments.append({
                "abs_start": ev["start-time"],
                "offset_seconds": (stt - ref_time).total_seconds(),
                "section": seg.get("timeline-section-title", ""),
                "title": seg.get("timeline-segment-title", ""),
            })

    attendance = []
    for cu in data.get("class-users") or []:
        if (cu.get("role") or "").lower() != "student":
            continue
        u = cu.get("user") or {}
        first = (u.get("first-name") or "").strip()
        last = (u.get("last-name") or "").strip()
        name = f"{first} {last}".strip() or (u.get("preferred-name") or "").strip() or first
        uid = u.get("id") or u.get("user-id")
        attendance.append({"id": uid, "name": name, "absent": bool(cu.get("absent", False))})

    timeline_segments.sort(key=lambda x: x["offset_seconds"])
    return {
        "class_id": class_id,
        "class_meta": class_meta,
        "voice_events": voice_events,
        "timeline_segments": timeline_segments,
        "attendance": attendance,
    }


def label_from_actor(actor, name_mode, student_ids=None):
    if not isinstance(actor, dict):
        return "Professor"
    uid = actor.get("id") or actor.get("user-id") or (actor.get("user") or {}).get("id")
    fn = (actor.get("first-name") or "").strip()
    ln = (actor.get("last-name") or "").strip()
    full = f"{fn} {ln}".strip()
    if name_mode == "ids":
        if (student_ids is None and uid is not None) or (student_ids is not None and uid in student_ids):
            return str(uid) if uid is not None else "ID"
    return full or "Professor"


# ------------------------------------------------------------------ #
#  GPU transcription function                                         #
# ------------------------------------------------------------------ #

@app.function(image=image, gpu="T4", timeout=7200, volumes={"/outputs": volume})
def run_transcription(
    job_id, class_id, class_url,
    forum_class_json, forum_events_json,
    signed_video_url, privacy_mode, whisper_model,
):
    import requests
    import torch
    import whisper
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch

    try:
        # 1 — Download video
        job_status[job_id] = {"status": "downloading", "message": "Downloading video …"}
        local_path = "/tmp/input.mp4"
        with requests.get(signed_video_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        # 2 — Convert to WAV
        job_status[job_id] = {"status": "preparing_audio", "message": "Converting to WAV …"}
        wav_path = "/tmp/input.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", local_path, "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path],
            check=True, capture_output=True,
        )

        # 3 — Whisper
        job_status[job_id] = {"status": "transcribing", "message": f"Transcribing with Whisper ({whisper_model}) — this may take a while …"}
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = whisper.load_model(whisper_model).to(device)
        if device == "cuda":
            model = model.half()
        result = model.transcribe(wav_path, word_timestamps=False, language="en", task="transcribe", fp16=(device == "cuda"))
        segments = result.get("segments", [])
        for s in segments:
            s["text"] = normalize_sentence_spacing(s.get("text", ""))

        # 4 — Parse forum data
        job_status[job_id] = {"status": "processing", "message": "Merging speaker data & compiling outputs …"}
        events_data = parse_forum_data(class_id, forum_class_json, forum_events_json)

        # 5 — Build output files
        out_dir = f"/outputs/job_{job_id}"
        os.makedirs(out_dir, exist_ok=True)

        voice_map = {}
        for ev in events_data.get("voice_events", []):
            voice_map[(ev["start"], ev["end"])] = ev.get("speaker", {})

        def find_speaker_at(t):
            for (s, e), sp in voice_map.items():
                if s <= t <= e:
                    return sp
            return {}

        student_ids_set = {a.get("id") for a in events_data.get("attendance", []) if a.get("id") is not None}
        fake_headers = {"referer": class_url or f"https://forum.minerva.edu/app/classes/{class_id}"}

        modes = ["names", "ids"] if privacy_mode == "both" else [privacy_mode]
        pdf_names, csv_names = [], []

        for mode in modes:
            # --- PDF ---
            pdf_name = f"session_{class_id}_transcript_{mode}.pdf"
            pdf_path = f"{out_dir}/{pdf_name}"
            styles = getSampleStyleSheet()
            contrib_style = ParagraphStyle('Contrib', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=12, wordWrap='CJK')
            header_style = ParagraphStyle('Header', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=12, textColor=colors.whitesmoke, alignment=1)
            speaker_style = ParagraphStyle('Speaker', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=12, wordWrap='CJK')

            doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=72)
            elems = []
            cm = events_data.get("class_meta", {})
            elems.append(Paragraph(cm.get("session_title", f"Session {class_id}"), styles["Title"]))
            sec = cm.get("section_title", "")
            if sec:
                elems.append(Paragraph(sec, ParagraphStyle("C", parent=styles["Heading3"], alignment=1)))
                elems.append(Spacer(1, 12))
            else:
                elems.append(Spacer(1, 12))

            left_style = ParagraphStyle("L", parent=styles["Normal"], alignment=0)
            dt_str = _fmt_dt_hm(cm.get("recording_start"))
            ref = fake_headers.get("referer", "")
            elems.append(Paragraph(f"<b>Class ID:</b> {class_id}", left_style))
            elems.append(Paragraph(f"<b>Class Date/Time:</b> {dt_str}", left_style))
            elems.append(Paragraph(f'<b>Class Link:</b> <a href="{ref}">{ref}</a>', left_style))
            elems.append(Spacer(1, 12))

            att = events_data.get("attendance", [])
            if att:
                elems.append(Paragraph("Attendance", styles["Heading3"]))
                att_rows = [[Paragraph("Student", header_style), Paragraph("Status", header_style)]]
                for a in att:
                    status = "Absent" if a.get("absent") else "Present"
                    display = f"ID {a.get('id')}" if mode == "ids" and a.get("id") is not None else a.get("name", "")
                    att_rows.append([Paragraph(display, speaker_style), status])
                att_table = Table(att_rows, colWidths=[4.5 * inch, 1.5 * inch], repeatRows=1)
                att_ts = TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 1), (-1, -1), 10),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 3),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ])
                for i, a in enumerate(att, start=1):
                    c = colors.red if a.get("absent") else colors.green
                    att_ts.add('TEXTCOLOR', (1, i), (1, i), c)
                att_table.setStyle(att_ts)
                elems.append(att_table)
                elems.append(Spacer(1, 18))

            sorted_segs = sorted(segments, key=lambda x: x.get("start", 0))
            rows = [[Paragraph("Time", header_style), Paragraph("Speaker", header_style), Paragraph("Contribution", header_style)]]
            for seg in sorted_segs:
                stt = float(seg.get("start", 0.0))
                sp = label_from_actor(find_speaker_at(stt), mode, student_ids_set)
                rows.append([_fmt_mmss(stt), Paragraph(sp, speaker_style), Paragraph(normalize_sentence_spacing(seg.get("text", "")), contrib_style)])
            table = Table(rows, colWidths=[0.85 * inch, 2.10 * inch, 4.05 * inch], repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 10),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]))
            elems.append(Paragraph("Transcript", styles["Heading3"]))
            elems.append(Spacer(1, 6))
            elems.append(table)
            doc.build(elems)
            pdf_names.append(pdf_name)

            # --- CSV ---
            csv_name = f"session_{class_id}_transcript_{mode}.csv"
            csv_path = f"{out_dir}/{csv_name}"
            csv_rows = []
            csv_rows.append(["Session", cm.get("session_title", "")])
            csv_rows.append(["Class ID", class_id])
            csv_rows.append(["Class Date/Time", dt_str])
            csv_rows.append(["Class Link", ref])
            csv_rows.append([])
            csv_rows.append(["Attendance"])
            csv_rows.append(["Student", "Status"])
            for a in att:
                status = "Absent" if a.get("absent") else "Present"
                display = f"ID {a.get('id')}" if mode == "ids" and a.get("id") is not None else a.get("name", "")
                csv_rows.append([display, status])
            csv_rows.append([])
            csv_rows.append(["Transcript"])
            csv_rows.append(["Time", "Speaker", "Contribution"])
            for seg in sorted_segs:
                stt = float(seg.get("start", 0.0))
                sp = label_from_actor(find_speaker_at(stt), mode, student_ids_set)
                csv_rows.append([_fmt_mmss(stt), sp, normalize_sentence_spacing(seg.get("text", ""))])
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for r in csv_rows:
                    w.writerow(r)
            csv_names.append(csv_name)

        volume.commit()

        meta = events_data.get("class_meta", {})
        job_status[job_id] = {
            "status": "complete",
            "message": "Transcription complete!",
            "files": {"pdfs": pdf_names, "csvs": csv_names},
            "meta": {
                "class_id": class_id,
                "session_title": meta.get("session_title", ""),
                "class_datetime": _fmt_dt_hm(meta.get("recording_start", "")),
            },
        }

    except Exception as exc:
        job_status[job_id] = {"status": "error", "message": str(exc)}


# ------------------------------------------------------------------ #
#  Web endpoints                                                       #
# ------------------------------------------------------------------ #

web_image = modal.Image.debian_slim().pip_install("flask", "flask-cors")

@app.function(image=web_image, allow_concurrent_inputs=100)
@modal.asgi_app()
def web():
    from flask import Flask, request, jsonify, send_file
    from flask_cors import CORS
    import io

    server = Flask(__name__)
    CORS(server, resources={r"/api/*": {"origins": "*"}})

    @server.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @server.route("/api/transcribe", methods=["POST"])
    def api_transcribe():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "No JSON body"}), 400

        required = ["class_id", "forum_class_json", "forum_events_json", "signed_video_url"]
        missing = [k for k in required if k not in data]
        if missing:
            return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

        jid = uuid.uuid4().hex[:8]
        job_status[jid] = {"status": "queued", "message": "Job queued …"}

        run_transcription.spawn(
            jid,
            data["class_id"],
            data.get("class_url", ""),
            data["forum_class_json"],
            data["forum_events_json"],
            data["signed_video_url"],
            data.get("privacy_mode", "names"),
            data.get("whisper_model", "medium"),
        )

        return jsonify({"job_id": jid, "status": "queued"})

    @server.route("/api/status/<jid>")
    def api_status(jid):
        entry = job_status.get(jid)
        if not entry:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"job_id": jid, **entry})

    @server.route("/api/download/<jid>/<filename>")
    def api_download(jid, filename):
        entry = job_status.get(jid)
        if not entry:
            return jsonify({"error": "Job not found"}), 404
        if entry.get("status") != "complete":
            return jsonify({"error": "Job not complete"}), 400
        filepath = f"/outputs/job_{jid}/{filename}"
        volume.reload()
        if not os.path.exists(filepath):
            return jsonify({"error": "File not found"}), 404
        mime = "application/pdf" if filename.endswith(".pdf") else "text/csv"
        return send_file(filepath, as_attachment=True, download_name=filename, mimetype=mime)

    return server
