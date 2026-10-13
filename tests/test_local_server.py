from pathlib import Path

import pytest

pytest.importorskip("flask")

from conftest import make_class  # noqa: E402

from hosts.local_server import JobStore, _Inline, create_app  # noqa: E402

EXT = "chrome-extension://" + "a" * 32
SIGNED = "https://dexample.cloudfront.net/u/rec.mp4?Expires=4102444800&Signature=SIGSECRET"
HOST = {"base_url": "http://127.0.0.1:5001"}


def fake_runner(bundle, *, out_dir, video_url, options, progress, job_id, asr_lock):
    progress("downloading", "Downloading video …", 0.5)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("json", "csv"):
        p = out_dir / f"class{bundle['class_id']}_{options.privacy}.{ext}"
        p.write_text("{}" if ext == "json" else "class_id\n1\n")
        files.append(p)
    progress("complete", "done", 1.0)
    return {"files": files, "summary": {"class_id": bundle["class_id"], "title": "T",
                                        "timing": "timing check: 0.9", "how_mix": {},
                                        "warnings": []}}


def failing_runner(bundle, **kw):
    raise RuntimeError(f"could not fetch {SIGNED}")


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "srv", runner=fake_runner, executor=_Inline())


def payload(**over):
    body = {"class_id": "900001", "signed_video_url": SIGNED, "forum_class_json": make_class(),
            "forum_events_json": [], "privacy_mode": "names"}
    body.update(over)
    return body


def test_health_and_cors_only_for_extensions(app):
    c = app.test_client()
    r = c.get("/api/health", headers={"Origin": EXT}, **HOST)
    assert r.status_code == 200 and r.json["version"] == "3.0.0"
    assert r.headers["Access-Control-Allow-Origin"] == EXT
    r = c.get("/api/health", headers={"Origin": "https://evil.example.com"}, **HOST)
    assert r.status_code == 403 and "Access-Control-Allow-Origin" not in r.headers
    r = c.options("/api/transcribe", headers={"Origin": EXT}, **HOST)
    assert r.status_code == 204 and r.headers["Access-Control-Allow-Origin"] == EXT
    r = c.get("/api/health", base_url="http://attacker.example:5001")
    assert r.status_code == 403  # DNS-rebinding guard


def test_transcribe_job_lifecycle_and_downloads(app):
    c = app.test_client()
    r = c.post("/api/transcribe", json=payload(), headers={"Origin": EXT}, **HOST)
    assert r.status_code == 200
    job = r.json["job_id"]
    assert len(job) >= 21  # token_urlsafe(16)
    s = c.get(f"/api/status/{job}", **HOST).json
    assert s["status"] == "complete"
    assert s["files"]["all"] == ["class900001_names.json", "class900001_names.csv"]
    assert s["files"]["csvs"] == ["class900001_names.csv"]
    r = c.get(f"/api/download/{job}/class900001_names.csv", **HOST)
    assert r.status_code == 200 and r.data.startswith(b"class_id")
    assert c.get(f"/api/download/{job}/..%2Fjobs.json", **HOST).status_code == 404
    assert c.get(f"/api/download/{job}/other.csv", **HOST).status_code == 404
    assert c.get("/api/status/nope", **HOST).status_code == 404
    store_text = (Path(app.config["SCRIBE_STORE"].path)).read_text()
    assert "SIGSECRET" not in store_text and "cloudfront" not in store_text


def test_v3_bundle_payload_and_validation(app):
    c = app.test_client()
    body = payload(forum_class_json=None, forum_events_json=None,
                   bundle={"class": make_class(), "events": [], "polls": []})
    assert c.post("/api/transcribe", json=body, **HOST).status_code == 200
    assert c.post("/api/transcribe", json=payload(class_id="abc"), **HOST).status_code == 400
    bad_url = payload(signed_video_url="http://169.254.169.254/latest")
    assert c.post("/api/transcribe", json=bad_url, **HOST).status_code == 400
    assert c.post("/api/transcribe", json=payload(privacy_mode="x"), **HOST).status_code == 400
    assert c.post("/api/transcribe", json=payload(formats="docx"), **HOST).status_code == 400
    assert c.post("/api/transcribe", data="x", **HOST).status_code == 400


def test_errors_are_redacted(tmp_path):
    app = create_app(tmp_path / "srv", runner=failing_runner, executor=_Inline())
    c = app.test_client()
    job = c.post("/api/transcribe", json=payload(), **HOST).json["job_id"]
    s = c.get(f"/api/status/{job}", **HOST).json
    assert s["status"] == "error" and "SIGSECRET" not in s["message"]


def test_jobs_survive_restart_and_running_jobs_are_marked_interrupted(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.create("done1", status="complete", files={"all": []})
    store.create("run1", status="transcribing")
    again = JobStore(tmp_path / "jobs.json")
    assert again.get("done1")["status"] == "complete"
    assert again.get("run1")["status"] == "error"
    assert "restart" in again.get("run1")["message"]
