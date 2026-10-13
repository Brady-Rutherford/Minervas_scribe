"""Localhost backend for extension_v3 (also accepts v2-style payloads).

    scribe-server [--port 5001] [--data-dir scribe_data/server]

Same endpoints the extension expects:
    GET  /api/health
    POST /api/transcribe        {class_id, signed_video_url, forum_class_json, forum_events_json,
                                 bundle?, privacy_mode?, formats?, provider?}
    GET  /api/status/<job_id>
    GET  /api/download/<job_id>/<file>

Hardening vs v2 (plan B6): binds 127.0.0.1 only; job ids are secrets.token_urlsafe(16);
CORS only for chrome-extension:// origins (pin yours with SCRIBE_ALLOWED_ORIGINS); Host header
must be localhost (blocks DNS rebinding); downloads limited to the job's own output files;
jobs persisted to jobs.json (no URLs or class data in it) so a restart doesn't lose them;
per-job temp workdirs; signed URLs never logged or stored.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file

from scribe_core import __version__
from scribe_core.export import parse_formats
from scribe_core.pipeline import TranscribeOptions, transcribe_bundle
from scribe_core.privacy import ensure_private_dir, load_or_create_salt
from scribe_core.redact import redact_urls
from scribe_core.timeutil import now_iso

log = logging.getLogger("scribe.server")

DEFAULT_PORT = 5001
TERMINAL = {"complete", "error"}
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MIME = {".pdf": "application/pdf", ".csv": "text/csv", ".json": "application/json",
        ".jsonl": "application/x-ndjson", ".md": "text/markdown", ".html": "text/html"}
STAGE_TO_STATUS = {"fetching": "queued", "downloading": "downloading",
                   "preparing_audio": "preparing_audio", "transcribing": "transcribing",
                   "processing": "processing", "compiling": "compiling"}
PUBLIC_FIELDS = ("job_id", "status", "message", "files", "meta", "created_at", "updated_at")


class JobStore:
    """Tiny JSON-file job table (thread-safe, atomic writes)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        try:
            self.jobs = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.jobs = {}
        changed = False
        for job in self.jobs.values():
            if job.get("status") not in TERMINAL:
                job.update(status="error", message="Interrupted by a server restart. Start again.")
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.jobs, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def create(self, job_id: str, **fields) -> None:
        with self.lock:
            now = now_iso()
            self.jobs[job_id] = {"job_id": job_id, "created_at": now, "updated_at": now, **fields}
            self._save()

    def update(self, job_id: str, **fields) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job.update(fields, updated_at=now_iso())
            self._save()

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None


class _Inline:
    """Executor that runs jobs synchronously (tests)."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def video_url_ok(url: str, allow_any_host: bool = False) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    return allow_any_host or parts.hostname.endswith(".cloudfront.net")


def create_app(data_dir: Path | str | None = None, *, runner=None, executor=None,
               allowed_origins: set[str] | None = None, allow_any_video_host: bool | None = None
               ) -> Flask:
    data_dir = ensure_private_dir(data_dir or Path("scribe_data") / "server")
    jobs_dir = data_dir / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    store = JobStore(data_dir / "jobs.json")
    salt = load_or_create_salt(data_dir / ".id_salt")
    asr_lock = threading.Lock()  # one local-Whisper job at a time; remote ASR runs in parallel
    pool = executor or ThreadPoolExecutor(max_workers=int(os.environ.get("SCRIBE_WORKERS", "3")))
    run = runner or transcribe_bundle
    if allowed_origins is None:
        env = os.environ.get("SCRIBE_ALLOWED_ORIGINS", "")
        allowed_origins = {o.strip() for o in env.split(",") if o.strip()}
    if allow_any_video_host is None:
        allow_any_video_host = os.environ.get("SCRIBE_ALLOW_ANY_VIDEO_HOST") == "1"

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    app.config["SCRIBE_STORE"] = store

    def origin_ok(origin: str) -> bool:
        if allowed_origins:
            return origin in allowed_origins
        return bool(EXTENSION_ORIGIN.match(origin))

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_HOSTS:
            return jsonify({"error": "Forbidden host"}), 403
        origin = request.headers.get("Origin")
        if origin and not origin_ok(origin):
            return jsonify({"error": "Origin not allowed"}), 403
        if request.method == "OPTIONS":
            return "", 204
        return None

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin")
        if origin and origin_ok(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Vary"] = "Origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def run_job(job_id: str, bundle: dict, url: str, opts: TranscribeOptions) -> None:
        def progress(stage, message, fraction=None):
            status = STAGE_TO_STATUS.get(stage)
            if status:  # "complete" is set below, once the file list exists
                store.update(job_id, status=status, message=message)

        try:
            result = run(bundle, out_dir=jobs_dir / job_id, video_url=url, options=opts,
                         progress=progress, job_id=job_id, asr_lock=asr_lock)
            names = [Path(f).name for f in result["files"]]
            s = result["summary"]
            store.update(
                job_id, status="complete", message="Transcription complete!",
                files={"all": names,
                       "pdfs": [n for n in names if n.endswith(".pdf")],
                       "csvs": [n for n in names if n.endswith(".csv")]},
                meta={"class_id": s.get("class_id"), "session_title": s.get("title"),
                      "timing": s.get("timing"), "how_mix": s.get("how_mix"),
                      "warnings": s.get("warnings")},
            )
        except Exception as exc:  # noqa: BLE001 - report every failure to the extension
            log.exception("job %s failed", job_id)
            store.update(job_id, status="error", message=redact_urls(str(exc))[:500])

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "version": __version__, "engine": "scribe_core"})

    @app.post("/api/transcribe")
    def api_transcribe():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        class_id = str(data.get("class_id", "")).strip()
        if not class_id.isdigit():
            return jsonify({"error": "class_id must be numeric"}), 400
        url = str(data.get("signed_video_url") or "")
        if not video_url_ok(url, allow_any_video_host):
            return jsonify({"error": "signed_video_url must be an https CloudFront URL"}), 400
        extra = data.get("bundle") if isinstance(data.get("bundle"), dict) else {}
        klass = data.get("forum_class_json") or extra.get("class")
        events = data.get("forum_events_json") or extra.get("events") or []
        if not isinstance(klass, dict) or not isinstance(events, list):
            return jsonify({"error": "forum_class_json (object) and forum_events_json (array) "
                                     "are required"}), 400
        privacy = data.get("privacy_mode") or "names"
        if privacy not in ("names", "ids", "both"):
            return jsonify({"error": "privacy_mode must be names, ids or both"}), 400
        provider = data.get("provider") or "auto"
        if provider not in ("auto", "deepgram", "groq", "local"):
            return jsonify({"error": "unknown provider"}), 400
        try:
            formats = parse_formats(data.get("formats"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        bundle = {**extra, "class": klass, "events": events, "class_id": int(class_id)}
        opts = TranscribeOptions(provider=provider, privacy=privacy, formats=tuple(formats),
                                 language=data.get("language"),
                                 salt=salt if privacy in ("ids", "both") else None)
        job_id = secrets.token_urlsafe(16)
        store.create(job_id, class_id=int(class_id), status="queued", message="Job queued …",
                     files=None, meta=None)
        pool.submit(run_job, job_id, bundle, url, opts)
        return jsonify({"job_id": job_id, "status": "queued"})

    @app.get("/api/status/<job_id>")
    def api_status(job_id):
        job = store.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({k: job.get(k) for k in PUBLIC_FIELDS})

    @app.get("/api/download/<job_id>/<filename>")
    def api_download(job_id, filename):
        job = store.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job.get("status") != "complete":
            return jsonify({"error": "Job not complete"}), 400
        if filename not in ((job.get("files") or {}).get("all") or []):
            return jsonify({"error": "File not found"}), 404
        base = (jobs_dir / job_id).resolve()
        path = (base / filename).resolve()
        if base not in path.parents or not path.is_file():
            return jsonify({"error": "File not found"}), 404
        mime = MIME.get(path.suffix, "application/octet-stream")
        return send_file(path, as_attachment=True, download_name=filename, mimetype=mime)

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scribe-server", description="Scribe v3 localhost backend")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--data-dir", type=Path, default=Path("scribe_data") / "server")
    p.add_argument("--env-file", type=Path)
    args = p.parse_args(argv)
    from dotenv import find_dotenv, load_dotenv

    env = str(args.env_file) if args.env_file else find_dotenv(usecwd=True)
    if env:
        load_dotenv(env, override=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(args.data_dir)
    print(f"Minerva's Scribe v3 backend on http://127.0.0.1:{args.port} (localhost only)")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
