import csv
import json

import pytest
from conftest import T0_MS, chat_event, make_class, voice_event

from hosts import cli
from scribe_core.forum import ForumAuthError, ForumHTTPError


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # Never let tests read a developer's real .env (it holds FORUM_COOKIE).
    monkeypatch.setattr(cli, "_load_env", lambda env_file: None)


def test_parser_subcommands_and_defaults():
    p = cli.build_parser()
    a = p.parse_args(["sync"])
    assert a.command == "sync" and a.sections == ["past", "current"] and a.limit == 0
    a = p.parse_args(["sync", "--out", "x", "--sections", "past", "--limit", "5"])
    assert str(a.out) == "x" and a.sections == ["past"] and a.limit == 5
    a = p.parse_args(["transcribe", "--class", "81321", "--provider", "groq", "--privacy", "ids",
                      "--formats", "json,csv,html", "--res", "highres", "--keyterm", "Bayes",
                      "--keyterm", "Hungarian"])
    assert (a.class_id, a.provider, a.privacy, a.res) == (81321, "groq", "ids", "highres")
    assert a.formats == ["json", "csv", "html"] and a.keyterm == ["Bayes", "Hungarian"]
    a = p.parse_args(["transcribe", "--class", "1"])
    assert a.provider == "auto" and a.formats == ["json", "csv", "md", "html", "jsonl"]
    assert p.parse_args(["probe", "--class", "7"]).res == "lowres"
    assert p.parse_args(["census", "--dir", "d"]).class_id is None
    for bad in (["sync", "--sections", "future"], ["transcribe", "--class", "1", "--formats",
                                                   "docx"], ["census"], ["probe"],
                ["transcribe", "--class", "1", "--provider", "whisperx"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


class FakeForum:
    """Stands in for ForumClient (no network)."""

    def __init__(self, fail_class=None):
        self.bundle_calls = []
        self.fail_class = fail_class

    def me(self):
        return {"id": 2, "first-name": "Alice", "last-name": "Example", "role": "student"}

    def sections(self, past=False):
        course = {"id": 5, "course-code": "SYN101", "term": {"name": "Spring 2099"}}
        return [{"id": 77, "course": course}] if past else [{"id": 78, "course": course}]

    def classes(self, sid):
        return [{"id": 1000 + sid * 10 + i, "title": f"Session {i}", "date": "2099-01-05",
                 "recording-sessions": []} for i in range(2)]

    def class_bundle(self, cid, extras=True):
        self.bundle_calls.append(cid)
        if cid == self.fail_class:
            raise ForumHTTPError(500, f"/classes/{cid}")
        klass = make_class()
        klass["id"] = cid
        events = [voice_event(2, T0_MS, 1000), voice_event(3, T0_MS + 5000, 1000),
                  chat_event(2, T0_MS + 9000, "hi")]
        return {"class": klass, "events": events, "polls": [], "poll_responses": None,
                "outcome_assessments": None, "focused_outcomes": [], "fetched_at": "now",
                "status": {"class": 200}}

    def class_events(self, cid):
        return self.class_bundle(cid)["events"]


def run_sync(tmp_path, forum, *extra):
    args = cli.build_parser().parse_args(["sync", "--out", str(tmp_path / "data"), *extra])
    return cli.cmd_sync(args, forum)


def test_sync_is_resumable_and_writes_manifest(tmp_path, capsys):
    forum = FakeForum()
    assert run_sync(tmp_path, forum, "--limit", "3") == 0
    assert len(forum.bundle_calls) == 3
    assert run_sync(tmp_path, forum) == 0
    assert len(forum.bundle_calls) == 4  # only the one missing class fetched the 2nd time
    data = tmp_path / "data"
    assert (data / ".gitignore").exists()  # the data dir ignores itself
    assert json.loads((data / "me.json").read_text()) == {"id": 2, "role": "student"}
    raw = data / "raw" / "1770"
    assert (raw / "class.json").exists() and (raw / "events.json").exists()
    assert (raw / "_complete.json").exists() and not (raw / "poll_responses.json").exists()
    rows = list(csv.DictReader((data / "manifest.csv").open()))
    assert len(rows) == 4
    assert list(rows[0]) == cli.MANIFEST_COLUMNS
    r = next(x for x in rows if x["class_id"] == "1770")
    assert r["section_id"] == "77" and r["course_code"] == "SYN101" and r["term"] == "Spring 2099"
    assert r["n_voice"] == "2" and r["n_my_voice"] == "1"
    assert json.loads(r["n_events_by_type"]) == {"message": 1, "voice": 2}
    assert r["recording_started"] == "2099-01-05T15:00:00Z"
    out = capsys.readouterr().out
    assert "already complete" in out


def test_sync_records_failures_and_retries_them(tmp_path):
    forum = FakeForum(fail_class=1771)
    assert run_sync(tmp_path, forum) == 1
    assert (tmp_path / "data" / "raw" / "1771" / "_error.json").exists()
    forum.fail_class = None
    assert run_sync(tmp_path, forum) == 0
    assert not (tmp_path / "data" / "raw" / "1771" / "_error.json").exists()


def test_sync_stops_on_dead_cookie(tmp_path):
    forum = FakeForum()

    def dead(cid, extras=True):
        raise ForumAuthError(401, "/x")

    forum.class_bundle = dead
    with pytest.raises(ForumAuthError):
        run_sync(tmp_path, forum)


def test_census_over_synced_dir_needs_no_cookie(tmp_path, capsys, monkeypatch):
    run_sync(tmp_path, FakeForum())
    monkeypatch.delenv("FORUM_COOKIE", raising=False)
    assert cli.main(["census", "--dir", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out
    assert "voice" in out and "message" in out


def test_main_reports_missing_cookie_cleanly(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("FORUM_COOKIE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["me"]) == 2
    assert "FORUM_COOKIE" in capsys.readouterr().err


def test_census_for_one_class(capsys):
    args = cli.build_parser().parse_args(["census", "--class", "5"])
    assert cli.cmd_census(args, FakeForum()) == 0
    out = capsys.readouterr().out
    assert "main room: room-main" in out


def test_offline_transcribe_reuses_saved_asr_without_cookie(tmp_path, monkeypatch, capsys):
    run_sync(tmp_path, FakeForum())
    asr = tmp_path / "x_asr.json"
    asr.write_text(json.dumps({
        "provider": "deepgram", "model": "nova-3", "diarized": True, "duration": 10.0,
        "words": [{"start": 0.1, "end": 0.5, "text": "Hello", "conf": 0.9, "cluster": 0},
                  {"start": 5.1, "end": 5.6, "text": "Hi!", "conf": 0.9, "cluster": 1}],
    }))
    monkeypatch.delenv("FORUM_COOKIE", raising=False)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    code = cli.main(["transcribe", "--class", "1770", "--bundle-dir",
                     str(tmp_path / "data" / "raw" / "1770"), "--reuse-asr", str(asr),
                     "--out", str(out), "--formats", "json", "--no-debug"])
    assert code == 0
    t = json.loads(next(out.glob("*_names.json")).read_text())
    assert [u["speaker_id"] for u in t["utterances"]] == ["2", "3"]
    assert t["asr"]["provider"] == "deepgram" and t["asr"]["mode"] == "saved"
    assert (tmp_path / "scribe_data" / ".gitignore").exists()


def test_caps_command_prints_endpoint_table(capsys):
    class Caps(FakeForum):
        def probe_capabilities(self, cid, sid):
            return {"user_id": 2, "role": "student",
                    "endpoints": {"analytics": 403, "breakouts": 200}}

    args = cli.build_parser().parse_args(["caps", "--class", "5", "--section", "77"])
    assert cli.cmd_caps(args, Caps()) == 0
    out = capsys.readouterr().out
    assert "analytics" in out and "no (403)" in out and "yes (200)" in out
