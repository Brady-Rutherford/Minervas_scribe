import re
import io
import os
import gc
import csv
import json
import math
import subprocess
from pathlib import Path
from datetime import timedelta
import requests
import iso8601
import numpy as np
from pydub import AudioSegment
from groq import Groq
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from typing import Optional, Set

# --------------------------
# Helpers mirroring notebook
# --------------------------

def parse_class_id_from_curl(curl_text: str) -> Optional[str]:
    m = re.search(r"/app/courses/\d+/sections/\d+/classes/(\d+)", curl_text)
    if m:
        return m.group(1)
    m2 = re.search(r"/api/v1/class_grader/classes/(\d+)", curl_text)
    return m2.group(1) if m2 else None

def extract_ids_and_link(curl_text: str):
    ref_match = re.search(r"-H\s+['\"](?:referer|Referer):\s*([^'\"\r\n]+)", curl_text)
    ref = ref_match.group(1).strip() if ref_match else ""
    course_id = section_id = class_id = None
    class_link = ""

    if ref:
        m = re.search(r"/app/courses/(\d+)/sections/(\d+)/classes/(\d+)", ref)
        if m:
            course_id, section_id, class_id = m.group(1), m.group(2), m.group(3)
            class_link = ref

    if not class_id:
        m2 = re.search(r"/api/v1/class_grader/classes/(\d+)", curl_text)
        if m2:
            class_id = m2.group(1)
            class_link = f"https://forum.minerva.edu/app/classes/{class_id}"

    return {"course_id": course_id, "section_id": section_id, "class_id": class_id, "class_link": class_link}

def clean_curl_headers(curl_string: str) -> dict:
    headers = {}
    for name, value in re.findall(r"-H ['\"](.*?): (.*?)['\"]", curl_string):
        headers[name] = value
    cookie_match = re.search(r"-b ['\"](.*?)['\"]", curl_string)
    if cookie_match:
        headers["Cookie"] = cookie_match.group(1)
    return headers

def download_to_temp(url: str, progress_cb=None) -> str:   # 👈 CHANGED for progress
    base = url.split("?", 1)[0]
    suffix = Path(base).suffix or ".mp4"
    local_name = f"tmp_inputs/input_from_url{suffix}"
    Path("tmp_inputs").mkdir(parents=True, exist_ok=True)

    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0

        with open(local_name, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:   # 👈 ADDED for progress
                        progress_cb("Downloading audio/video...", downloaded / total)

    return local_name
def normalize_sentence_spacing(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)
    text = text.replace('\u00A0', ' ')
    text = re.sub(r'\s*\n+\s*', ' ', text)
    text = re.sub(r'(\.\.\.)(?=\S)', r'\1 ', text)
    text = re.sub(r'(?<!\.)' r'([.!?])' r'(?=(["“\'(\[]?[A-Za-z]))', r'\1 ', text)
    text = re.sub(r'([:;])(?=(["“\'(\[]?[A-Za-z]))', r'\1 ', text)
    text = re.sub(r'([.!?]["”\')\]])(?=\S)', r'\1 ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def _fmt_mmss(seconds_float):
    if seconds_float is None:
        return ""
    seconds = max(0, int(seconds_float))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

def _safe_date(date_str):
    if not date_str:
        return ""
    try:
        return date_str.split("T")[0]
    except Exception:
        return ""

def _fmt_dt_hm(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = iso8601.parse_date(dt_str)
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return _safe_date(dt_str)

# --------------------------
# Audio prep / conversion
# --------------------------
def convert_to_whisper_wav(audio_path: str) -> str:
    wav_path = str(Path(audio_path).with_suffix(".wav"))
    cmd = [
        "ffmpeg", "-y", "-i", audio_path,
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        wav_path
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return wav_path

def validate_and_prepare_audio(input_path: str) -> str:
    p = Path(input_path)
    if not p.exists():
        raise FileNotFoundError(f"Audio path not found: {input_path}")
    if p.suffix.lower() in [".wav"]:
        return str(p)
    return convert_to_whisper_wav(str(p))

# --------------------------
# Transcription (Groq API)
# --------------------------
def transcribe_to_json(wav_path: str, class_id: str, model_name: str = "whisper-large-v3") -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set. Get a free key at console.groq.com")

    client = Groq(api_key=api_key)

    MAX_FILE_SIZE_BYTES = 24 * 1024 * 1024  # stay under Groq's 25MB limit
    file_size = os.path.getsize(wav_path)

    if file_size <= MAX_FILE_SIZE_BYTES:
        with open(wav_path, "rb") as f:
            response = client.audio.transcriptions.create(
                file=(Path(wav_path).name, f.read()),
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
                language="en",
                timestamp_granularities=["segment"],
            )
        raw_segs = response.segments if hasattr(response, "segments") else response.get("segments", [])
        segments = []
        for seg in raw_segs:
            s = seg if isinstance(seg, dict) else seg.__dict__ if hasattr(seg, "__dict__") else {}
            start = s.get("start", 0) if isinstance(s, dict) else getattr(seg, "start", 0)
            end = s.get("end", 0) if isinstance(s, dict) else getattr(seg, "end", 0)
            text = s.get("text", "") if isinstance(s, dict) else getattr(seg, "text", "")
            segments.append({"start": start, "end": end, "text": normalize_sentence_spacing(str(text).strip())})
    else:
        audio = AudioSegment.from_wav(wav_path)
        total_duration_ms = len(audio)
        chunk_duration_ms = int((MAX_FILE_SIZE_BYTES / file_size) * total_duration_ms * 0.9)
        overlap_ms = 5000

        segments = []
        offset_ms = 0
        chunk_index = 0

        while offset_ms < total_duration_ms:
            end_ms = min(offset_ms + chunk_duration_ms, total_duration_ms)
            chunk = audio[offset_ms:end_ms]

            chunk_path = f"tmp_inputs/chunk_{class_id}_{chunk_index}.wav"
            Path("tmp_inputs").mkdir(exist_ok=True)
            chunk.export(chunk_path, format="wav")

            with open(chunk_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    file=(f"chunk_{chunk_index}.wav", f.read()),
                    model="whisper-large-v3-turbo",
                    response_format="verbose_json",
                    language="en",
                    timestamp_granularities=["segment"],
                )

            offset_seconds = offset_ms / 1000.0
            skip_seconds = (overlap_ms / 1000.0) if chunk_index > 0 else 0

            raw_segs = response.segments if hasattr(response, "segments") else response.get("segments", [])
            for seg in raw_segs:
                s = seg if isinstance(seg, dict) else seg.__dict__ if hasattr(seg, "__dict__") else {}
                start = s.get("start", 0) if isinstance(s, dict) else getattr(seg, "start", 0)
                end = s.get("end", 0) if isinstance(s, dict) else getattr(seg, "end", 0)
                text = s.get("text", "") if isinstance(s, dict) else getattr(seg, "text", "")
                if start >= skip_seconds:
                    segments.append({
                        "start": start + offset_seconds,
                        "end": end + offset_seconds,
                        "text": normalize_sentence_spacing(str(text).strip()),
                    })

            Path(chunk_path).unlink(missing_ok=True)
            offset_ms += chunk_duration_ms - overlap_ms
            chunk_index += 1

    result = {"segments": segments}
    out_path = f"outputs/session_{class_id}_transcript.json"
    Path("outputs").mkdir(exist_ok=True, parents=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return out_path
# --------------------------
# Forum metadata / events
# --------------------------
def get_forum_events(class_id: str, headers: dict) -> dict:
    class_url = f"https://forum.minerva.edu/api/v1/class_grader/classes/{class_id}"
    r = requests.get(class_url, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()

    session_title = data.get("title") or f"Session {class_id}"
    course_obj = (data.get("section") or {}).get("course") or {}
    course_code  = course_obj.get("course-code", "")
    course_title = course_obj.get("title", "")
    section_title = (data.get("section") or {}).get("title", "")
    class_type = data.get("type", "")
    rec = (data.get("recording-sessions") or [{}])[0]
    recording_start = rec.get("recording-started")
    recording_end   = rec.get("recording-ended")

    schedule_guess = ""
    if isinstance(section_title, str) and "," in section_title:
        parts = [p.strip() for p in section_title.split(",", 1)]
        schedule_guess = parts[1] if len(parts) > 1 else ""

    class_meta = {
        "session_title": session_title,
        "course_code": course_code,
        "course_title": course_title,
        "section_title": section_title,
        "schedule": schedule_guess,
        "class_type": class_type,
        "recording_start": recording_start,
        "recording_end": recording_end,
    }

    events_url = f"https://forum.minerva.edu/api/v1/class_grader/classes/{class_id}/class-events"
    r2 = requests.get(events_url, headers=headers, timeout=60)
    r2.raise_for_status()
    events = r2.json() if isinstance(r2.json(), list) else []

    ref_time = iso8601.parse_date(recording_start) if recording_start else None
    voice_events = []
    timeline_segments = []

    for ev in events:
        et = ev.get("event-type")
        if et == "voice" and ref_time:
            duration_ms = (ev.get("event-data") or {}).get("duration", 0)
            duration = duration_ms / 1000.0
            if duration >= 1:
                stt = iso8601.parse_date(ev["start-time"])
                ett = iso8601.parse_date(ev["end-time"])
                voice_events.append({
                    "start": (stt - ref_time).total_seconds(),
                    "end": (ett - ref_time).total_seconds(),
                    "duration": duration,
                    "speaker": {
                        "id": (ev.get("actor") or {}).get("id") or (ev.get("actor") or {}).get("user-id"),
                        "first-name": (ev.get("actor") or {}).get("first-name"),
                        "last-name": (ev.get("actor") or {}).get("last-name")
                    }
                })
        elif et == "timeline-segment" and ref_time:
            stt = iso8601.parse_date(ev["start-time"])
            seg = (ev.get("event-data") or {})
            timeline_segments.append({
                "abs_start": ev["start-time"],
                "offset_seconds": (stt - ref_time).total_seconds(),
                "section": seg.get("timeline-section-title", ""),
                "title": seg.get("timeline-segment-title", ""),
            })

    attendance = []
    for cu in (data.get("class-users") or []):
        role = (cu.get("role") or "").lower()
        if role == "student":
            u = cu.get("user") or {}
            first = (u.get("first-name") or "").strip()
            last = (u.get("last-name") or "").strip()
            name = (first + " " + last).strip() or (u.get("preferred-name") or "").strip() or (u.get("first-name") or "").strip()
            uid = u.get("id") or u.get("user-id")
            absent = bool(cu.get("absent", False))
            attendance.append({"id": uid, "name": name, "absent": absent})

    timeline_segments.sort(key=lambda x: x["offset_seconds"])
    events_data = {
        "class_id": class_id,
        "class_meta": class_meta,
        "voice_events": voice_events,
        "timeline_segments": timeline_segments,
        "attendance": attendance
    }

    Path("outputs").mkdir(parents=True, exist_ok=True)
    with open(f"outputs/session_{class_id}_events.json", "w", encoding="utf-8") as f:
        json.dump(events_data, f, indent=2)
    return events_data
# --------------------------
# Output compilers
# --------------------------
def label_from_actor(actor: dict, name_mode: str, student_ids: Optional[Set[int]] = None) -> str:
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

def compile_pdf(class_id: str, headers: dict, name_mode: str, out_dir: str) -> str:
    with open(f"outputs/session_{class_id}_transcript.json", "r", encoding="utf-8") as f:
        transcript_data = json.load(f)
    with open(f"outputs/session_{class_id}_events.json", "r", encoding="utf-8") as f:
        events_data = json.load(f)

    styles = getSampleStyleSheet()
    contribution_style = ParagraphStyle('ContributionStyle', parent=styles['Normal'],
                                        fontName='Helvetica', fontSize=10, leading=12, wordWrap='CJK')
    header_style = ParagraphStyle('HeaderStyle', parent=styles['Normal'],
                                  fontName='Helvetica-Bold', fontSize=12, textColor=colors.whitesmoke, alignment=1)
    speaker_style = ParagraphStyle('SpeakerStyle', parent=styles['Normal'],
                                   fontName='Helvetica', fontSize=10, leading=12, wordWrap='CJK')

    output_path = str(Path(out_dir) / f"session_{class_id}_transcript_{name_mode}.pdf")
    doc = SimpleDocTemplate(output_path, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=72)
    elements = []

    # Header block
    class_meta = events_data.get("class_meta", {})
    session_line = class_meta.get("session_title") or f"Session {class_id}"
    elements.append(Paragraph(session_line, styles["Title"]))

    sec_sched = class_meta.get("section_title", "") or class_meta.get("schedule", "")
    if sec_sched:
        centered = ParagraphStyle("CenteredInfo", parent=styles["Heading3"], alignment=1)
        elements.append(Paragraph(sec_sched, centered))
        elements.append(Spacer(1, 12))
    else:
        elements.append(Spacer(1, 12))

    left_info_style = ParagraphStyle("LeftInfo", parent=styles["Normal"], alignment=0)
    class_datetime = _fmt_dt_hm(class_meta.get("recording_start"))
    ref = (headers.get("referer") or headers.get("Referer") or "").strip()
    m = re.search(r"https://forum\.minerva\.edu/app/[^\s\"']+", ref)
    class_link = m.group(0) if m else f"https://forum.minerva.edu/app/classes/{class_id}"
    elements.append(Paragraph(f"<b>Class ID:</b> {class_id}", left_info_style))
    elements.append(Paragraph(f"<b>Class Date/Time:</b> {class_datetime}", left_info_style))
    elements.append(Paragraph(f'<b>Class Link:</b> <a href="{class_link}">{class_link}</a>', left_info_style))
    elements.append(Spacer(1, 12))

    # Attendance
    attendance = events_data.get("attendance", [])
    if attendance:
        elements.append(Paragraph("Attendance", styles["Heading3"]))
        att_rows = [[Paragraph("Student", header_style), Paragraph("Status", header_style)]]
        student_ids_set = {a.get("id") for a in attendance if a.get("id") is not None}
        for a in attendance:
            status = "Absent" if a.get("absent") else "Present"
            display_student = (f"ID {a.get('id')}" if name_mode == "ids" and a.get("id") is not None else a.get("name",""))
            att_rows.append([Paragraph(display_student, speaker_style), status])
        att_table = Table(att_rows, colWidths=[4.5*inch, 1.5*inch], repeatRows=1)
        att_style = TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.grey),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('ALIGN', (0,0), (-1,0), 'CENTER'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,1), (-1,-1), 10),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('GRID', (0,0), (-1,-1), 1, colors.black),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ])
        # color the status column
        for i, a in enumerate(attendance, start=1):
            color = colors.red if a.get("absent") else colors.green
            att_style.add('TEXTCOLOR', (1,i), (1,i), color)
        att_table.setStyle(att_style)
        elements.append(att_table)
        elements.append(Spacer(1, 18))

    # Transcript table (simple, sorted by start)
    segs = sorted(transcript_data.get("segments", []), key=lambda x: x.get("start", 0))
    rows = [[Paragraph("Time", header_style), Paragraph("Speaker", header_style), Paragraph("Contribution", header_style)]]

    # make a quick speaker index using voice windows
    voice_map = {}
    for ev in events_data.get("voice_events", []):
        voice_map[(ev["start"], ev["end"])] = ev.get("speaker", {})

    def find_speaker_at(t):
        # simple scan
        for (s, e), sp in voice_map.items():
            if s <= t <= e:
                return sp
        return {}

    student_ids_set = {a.get("id") for a in events_data.get("attendance", []) if a.get("id") is not None}

    for seg in segs:
        stt = float(seg.get("start", 0.0))
        speaker_txt = label_from_actor(find_speaker_at(stt), name_mode, student_ids_set)
        rows.append([
            _fmt_mmss(stt),
            Paragraph(speaker_txt, speaker_style),
            Paragraph(normalize_sentence_spacing(seg.get("text","")), contribution_style)
        ])

    table = Table(rows, colWidths=[0.85*inch, 2.10*inch, 4.05*inch], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,1), (-1,-1), 10),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))

    elements.append(Paragraph("Transcript", styles["Heading3"]))
    elements.append(Spacer(1,6))
    elements.append(table)
    elements.append(Spacer(1, 12))

    doc.build(elements)
    return output_path

def compile_csv(class_id: str, headers: dict, name_mode: str, out_dir: str) -> str:
    with open(f"outputs/session_{class_id}_transcript.json", "r", encoding="utf-8") as f:
        transcript_data = json.load(f)
    with open(f"outputs/session_{class_id}_events.json", "r", encoding="utf-8") as f:
        events_data = json.load(f)

    output_path = str(Path(out_dir) / f"session_{class_id}_transcript_{name_mode}.csv")
    rows = []

    class_meta = events_data.get("class_meta", {})
    session_line = class_meta.get("session_title","")
    sec_sched = class_meta.get("section_title") or class_meta.get("schedule") or ""
    class_datetime = _fmt_dt_hm(class_meta.get("recording_start"))

    # Header block
    rows.append(["Session", session_line])
    if sec_sched:
        rows.append([sec_sched])
    rows.append([])
    rows.append(["Class ID", parse_class_id_from_curl(json.dumps(headers)) or ""])
    rows.append(["Class Date/Time", class_datetime])
    ref = (headers.get("referer") or headers.get("Referer") or "").strip()
    m = re.search(r"https://forum\.minerva\.edu/app/[^\s\"']+", ref)
    class_link = m.group(0) if m else ""
    rows.append(["Class Link", class_link])
    rows.append([])

    # Attendance
    rows.append(["Attendance"])
    rows.append(["Student","Status"])
    for a in events_data.get("attendance", []):
        status = "Absent" if a.get("absent") else "Present"
        display_student = (f"ID {a.get('id')}" if name_mode == "ids" and a.get("id") is not None else a.get("name",""))
        rows.append([display_student, status])
    rows.append([])

    # Transcript
    rows.append(["Transcript"])
    rows.append(["Time", "Speaker", "Contribution"])

    # simple speaker mapping
    voice_map = {}
    for ev in events_data.get("voice_events", []):
        voice_map[(ev["start"], ev["end"])] = ev.get("speaker", {})

    def find_speaker_at(t):
        for (s, e), sp in voice_map.items():
            if s <= t <= e:
                return sp
        return {}

    student_ids_set = {a.get("id") for a in events_data.get("attendance", []) if a.get("id") is not None}

    for seg in sorted(transcript_data.get("segments", []), key=lambda x: x.get("start", 0)):
        stt = float(seg.get("start", 0.0))
        speaker_txt = label_from_actor(find_speaker_at(stt), name_mode, student_ids_set)
        rows.append([_fmt_mmss(stt), speaker_txt, normalize_sentence_spacing(seg.get("text",""))])

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)
    return output_path
# --------------------------
# Main pipeline
# --------------------------
def run_pipeline(
    class_url: str,
    curl: str,
    audio_url: str,
    audio_local_path: Optional[str],
    privacy_mode: str,
    output_root: str = "outputs",
    model_name: str = "medium",
    progress_cb=None   # 👈 ADDED for progress
):
    # Determine class ID + headers
    ids = extract_ids_and_link(curl)
    class_id = ids.get("class_id")
    if not class_id:
        class_id = parse_class_id_from_curl(curl)
    if not class_id:
        raise ValueError("Could not extract Class ID from cURL.")

    headers = clean_curl_headers(curl)

    # Resolve audio path
    if audio_local_path:
        input_path = audio_local_path
    elif audio_url:
        if progress_cb:  # 👈 ADDED
            progress_cb("Downloading audio/video...", 0.0)
        input_path = download_to_temp(
            audio_url,
            progress_cb=progress_cb
        )  # 👈 CHANGED (pass progress into download)
        if progress_cb:  # 👈 ADDED
            progress_cb("Download complete", 1.0)
    else:
        raise ValueError("Provide an audio URL or upload a file.")

    # Prepare audio & transcribe
    if progress_cb:  # 👈 ADDED
        progress_cb("Preparing audio...", 0.3)
    wav_path = validate_and_prepare_audio(input_path)

    if progress_cb:  # 👈 ADDED
        progress_cb("Transcribing via Groq API (usually under 30 seconds)...", 0.5)
    transcript_json = transcribe_to_json(wav_path, class_id, model_name=model_name)  # <-- model param passed

    # Forum metadata + events
    if progress_cb:  # 👈 ADDED
        progress_cb("Fetching class metadata...", 0.8)
    events = get_forum_events(class_id, headers)

    # Compile outputs
    if progress_cb:  # 👈 ADDED
        progress_cb("Compiling outputs...", 0.9)
    out_dir = Path(output_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = []
    csvs = []

    modes = ["names", "ids"] if privacy_mode == "both" else [privacy_mode]
    for m in modes:
        pdfs.append(compile_pdf(class_id, headers, m, str(out_dir)))
        csvs.append(compile_csv(class_id, headers, m, str(out_dir)))

    if progress_cb:  # 👈 ADDED
        progress_cb("Finished!", 1.0)

    meta = {
        "class_id": class_id,
        "session_title": events.get("class_meta", {}).get("session_title"),
        "class_datetime": _fmt_dt_hm(events.get("class_meta", {}).get("recording_start")),
        "class_link": class_url or ids.get("class_link"),
    }

    return {"pdf_paths": pdfs, "csv_paths": csvs, "meta": meta}
