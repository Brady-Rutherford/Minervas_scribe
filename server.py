"""
server.py — Flask backend for the Class Transcriber Chrome extension.

Receives pre-fetched Forum JSON + signed video URL from the extension,
then runs:  download → ffmpeg → Whisper → merge → PDF/CSV compilation.

Usage:
    source .venv/bin/activate
    python server.py            # listens on http://0.0.0.0:5000
"""

import json
import uuid
import threading
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import iso8601

from logic import (
    download_to_temp,
    validate_and_prepare_audio,
    transcribe_to_json,
    compile_pdf,
    compile_csv,
    _fmt_dt_hm,
)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

jobs: dict = {}


# ------------------------------------------------------------------ #
#  Parse pre-fetched Forum JSON (mirrors logic.get_forum_events but   #
#  without any HTTP calls — data is already provided by the extension) #
# ------------------------------------------------------------------ #

def parse_forum_data(class_id, class_json, events_list):
    data = class_json

    session_title  = data.get("title") or f"Session {class_id}"
    course_obj     = (data.get("section") or {}).get("course") or {}
    course_code    = course_obj.get("course-code", "")
    course_title   = course_obj.get("title", "")
    section_title  = (data.get("section") or {}).get("title", "")
    class_type     = data.get("type", "")
    rec            = (data.get("recording-sessions") or [{}])[0]
    recording_start = rec.get("recording-started")
    recording_end   = rec.get("recording-ended")

    schedule_guess = ""
    if isinstance(section_title, str) and "," in section_title:
        parts = [p.strip() for p in section_title.split(",", 1)]
        schedule_guess = parts[1] if len(parts) > 1 else ""

    class_meta = {
        "session_title":   session_title,
        "course_code":     course_code,
        "course_title":    course_title,
        "section_title":   section_title,
        "schedule":        schedule_guess,
        "class_type":      class_type,
        "recording_start": recording_start,
        "recording_end":   recording_end,
    }

    ref_time = iso8601.parse_date(recording_start) if recording_start else None
    voice_events      = []
    timeline_segments  = []
    events = events_list if isinstance(events_list, list) else []

    for ev in events:
        et = ev.get("event-type")
        if et == "voice" and ref_time:
            duration_ms = (ev.get("event-data") or {}).get("duration", 0)
            duration = duration_ms / 1000.0
            if duration >= 1:
                stt = iso8601.parse_date(ev["start-time"])
                ett = iso8601.parse_date(ev["end-time"])
                voice_events.append({
                    "start":    (stt - ref_time).total_seconds(),
                    "end":      (ett - ref_time).total_seconds(),
                    "duration": duration,
                    "speaker": {
                        "id":         (ev.get("actor") or {}).get("id") or (ev.get("actor") or {}).get("user-id"),
                        "first-name": (ev.get("actor") or {}).get("first-name"),
                        "last-name":  (ev.get("actor") or {}).get("last-name"),
                    },
                })
        elif et == "timeline-segment" and ref_time:
            stt = iso8601.parse_date(ev["start-time"])
            seg = ev.get("event-data") or {}
            timeline_segments.append({
                "abs_start":      ev["start-time"],
                "offset_seconds": (stt - ref_time).total_seconds(),
                "section":        seg.get("timeline-section-title", ""),
                "title":          seg.get("timeline-segment-title", ""),
            })

    attendance = []
    for cu in data.get("class-users") or []:
        if (cu.get("role") or "").lower() != "student":
            continue
        u     = cu.get("user") or {}
        first = (u.get("first-name") or "").strip()
        last  = (u.get("last-name") or "").strip()
        name  = (
            f"{first} {last}".strip()
            or (u.get("preferred-name") or "").strip()
            or first
        )
        uid    = u.get("id") or u.get("user-id")
        absent = bool(cu.get("absent", False))
        attendance.append({"id": uid, "name": name, "absent": absent})

    timeline_segments.sort(key=lambda x: x["offset_seconds"])

    return {
        "class_id":           class_id,
        "class_meta":         class_meta,
        "voice_events":       voice_events,
        "timeline_segments":  timeline_segments,
        "attendance":         attendance,
    }


# ------------------------------------------------------------------ #
#  Background job runner                                               #
# ------------------------------------------------------------------ #

def run_job(
    job_id, class_id, class_url,
    forum_class_json, forum_events_json,
    signed_video_url, privacy_mode, whisper_model,
):
    try:
        jobs[job_id]["status"]  = "downloading"
        jobs[job_id]["message"] = "Downloading video …"
        input_path = download_to_temp(signed_video_url)

        jobs[job_id]["status"]  = "preparing_audio"
        jobs[job_id]["message"] = "Converting to WAV …"
        wav_path = validate_and_prepare_audio(input_path)

        jobs[job_id]["status"]  = "transcribing"
        jobs[job_id]["message"] = f"Transcribing with Whisper ({whisper_model}) — this may take a while …"
        transcribe_to_json(wav_path, class_id, model_name=whisper_model)

        jobs[job_id]["status"]  = "processing"
        jobs[job_id]["message"] = "Merging speaker data & compiling outputs …"

        events_data = parse_forum_data(class_id, forum_class_json, forum_events_json)
        Path("outputs").mkdir(parents=True, exist_ok=True)
        with open(f"outputs/session_{class_id}_events.json", "w", encoding="utf-8") as f:
            json.dump(events_data, f, indent=2)

        out_dir = f"outputs/job_{job_id}"
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        fake_headers = {
            "referer": class_url or f"https://forum.minerva.edu/app/classes/{class_id}"
        }

        pdfs, csvs = [], []
        modes = ["names", "ids"] if privacy_mode == "both" else [privacy_mode]
        for mode in modes:
            pdfs.append(compile_pdf(class_id, fake_headers, mode, out_dir))
            csvs.append(compile_csv(class_id, fake_headers, mode, out_dir))

        meta = events_data.get("class_meta", {})
        jobs[job_id].update({
            "status":  "complete",
            "message": "Transcription complete!",
            "files":   {
                "pdfs": [Path(p).name for p in pdfs],
                "csvs": [Path(c).name for c in csvs],
            },
            "out_dir": out_dir,
            "meta": {
                "class_id":       class_id,
                "session_title":  meta.get("session_title", ""),
                "class_datetime": _fmt_dt_hm(meta.get("recording_start", "")),
            },
        })

    except Exception as exc:
        jobs[job_id]["status"]  = "error"
        jobs[job_id]["message"] = str(exc)


# ------------------------------------------------------------------ #
#  API routes                                                          #
# ------------------------------------------------------------------ #

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    required = ["class_id", "forum_class_json", "forum_events_json", "signed_video_url"]
    missing  = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "job_id":  job_id,
        "status":  "queued",
        "message": "Job queued …",
        "files":   None,
        "meta":    None,
    }

    threading.Thread(
        target=run_job,
        args=(
            job_id,
            data["class_id"],
            data.get("class_url", ""),
            data["forum_class_json"],
            data["forum_events_json"],
            data["signed_video_url"],
            data.get("privacy_mode", "names"),
            data.get("whisper_model", "medium"),
        ),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    safe = {k: v for k, v in job.items() if k != "out_dir"}
    return jsonify(safe)


@app.route("/api/download/<job_id>/<filename>")
def api_download(job_id, filename):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    out_dir = job.get("out_dir")
    if not out_dir:
        return jsonify({"error": "Job not complete yet"}), 400
    filepath = Path(out_dir) / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    mime = "application/pdf" if filename.endswith(".pdf") else "text/csv"
    return send_file(str(filepath.resolve()), as_attachment=True, download_name=filename, mimetype=mime)


# ------------------------------------------------------------------ #

if __name__ == "__main__":
    Path("outputs").mkdir(exist_ok=True)
    Path("tmp_inputs").mkdir(exist_ok=True)
    print("Class Transcriber backend starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
