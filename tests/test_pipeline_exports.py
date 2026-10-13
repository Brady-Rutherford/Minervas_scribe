"""End-to-end on synthetic data: bundle + fake diarizing ASR -> transcript v2 -> all exports."""

import csv
import json
import re

import pytest
from conftest import (
    T0_MS,
    FakeResponse,
    chat_event,
    make_class,
    timeline_event,
    voice_event,
    words_from_intervals,
)

from scribe_core.asr_base import ASRProvider, ASRResult
from scribe_core.export import parse_formats
from scribe_core.export.to_csv import COLUMNS
from scribe_core.pipeline import PipelineError, TranscribeOptions, transcribe_bundle
from scribe_core.schema import UNATTRIBUTED

SIGNED = "https://dexample.cloudfront.net/u/rec.mp4?Expires=4102444800&Signature=SIGSECRET"
OFFSET = 0.8  # audio runs 0.8 s ahead of Forum's recording-started


class FakeDiarizingASR(ASRProvider):
    name = "fake"
    diarizes = True
    accepts_url = True

    def __init__(self, words):
        super().__init__("fake-1")
        self.words = words
        self.calls = []

    def transcribe(self, *, audio_path=None, video_url=None, audio_fallback=None, keyterms=(),
                   language=None):
        self.calls.append({"video_url": video_url, "keyterms": list(keyterms)})
        return ASRResult(words=[dict(w) for w in self.words], provider="fake", model="fake-1",
                         duration=400.0, diarized=True, mode="url")


def scenario():
    """Prof (1) talks, Alice (2) answers, Bob (3) interjects, plus a breakout and chat."""
    turns = [(1, 10.0, 40.0), (2, 42.0, 55.0), (3, 55.6, 56.2), (1, 57.0, 90.0),
             (2, 95.0, 120.0), (1, 125.0, 300.0)]
    events = []
    for uid, s, e in turns:
        # Forum splits long speech into fragments (median ~3 s).
        t = s
        while t < e:
            d = min(e - t, 3.0)
            events.append(voice_event(uid, T0_MS + int(t * 1000), int(d * 1000)))
            t += d + 0.15
    events += [voice_event(3, T0_MS + 200_000, 5_000, room="breakout-9")]
    events += [timeline_event("Warm-up", T0_MS, 1), timeline_event("Breakouts", T0_MS + 200_000, 2)]
    events += [chat_event(2, T0_MS + 60_000, "Link to the reading <b>please</b>")]
    cluster = {1: 0, 2: 1, 3: 2}
    speech = [(s + OFFSET, e + OFFSET, cluster[uid]) for uid, s, e in turns]
    speech.append((310.0, 314.0, 5))  # someone off-mic: no Forum voice, unmapped cluster
    words = words_from_intervals(speech)
    words[3]["filler"] = True
    words[3]["text"] = "um"
    bundle = {"schema": "scribe.bundle/v1", "class_id": 900001, "class": make_class(),
              "events": events, "polls": [], "me": {"id": 2}}
    return bundle, words


def run(tmp_path, privacy="names", formats=("json", "csv", "md", "html", "jsonl"), **kw):
    bundle, words = scenario()
    asr = FakeDiarizingASR(words)
    opts = TranscribeOptions(privacy=privacy, formats=formats, salt="test-salt", **kw)
    result = transcribe_bundle(bundle, out_dir=tmp_path, video_url=SIGNED, provider=asr,
                               options=opts)
    return result, asr


def test_pipeline_calibrates_fuses_and_writes_every_format(tmp_path):
    result, asr = run(tmp_path)
    t = result["transcripts"]["names"]
    assert asr.calls[0]["video_url"] == SIGNED  # URL mode: provider fetched it, no download
    assert asr.calls[0]["keyterms"][:2] == ["Prof Example", "Alice Example"]
    cal = t["alignment"]["calibration"]
    assert cal["applied"] and cal["offset"] == pytest.approx(OFFSET, abs=0.08)
    assert t["alignment"]["timing"].startswith("timing check")
    # Bob's 0.6 s "yeah" is too little evidence to map his cluster (min 1 s), but the
    # Forum voice event still credits him (short turns are no longer dropped, plan T4).
    assert t["alignment"]["cluster_map"] == {"0": "1", "1": "2"}
    assert t["alignment"]["main_room_id"] == "room-main"

    who = [(u["speaker_id"], round(u["start"])) for u in t["utterances"]]
    assert who[:4] == [("1", 11), ("2", 43), ("3", 56), ("1", 58)]
    assert who[-1] == (None, 310)  # off-mic voice: Unattributed, never "Professor"
    assert t["speakers"]["2"]["is_me"] is True and t["speakers"]["2"]["name"] == "Alice Example"
    assert t["speakers"]["1"]["role"] == "professor"
    assert t["speakers"]["3"]["breakout_talk_s"] == pytest.approx(5.0)
    assert t["speakers"]["3"]["talk_s"] == pytest.approx(0.6, abs=0.01)  # main room only
    assert [c["title"] for c in t["timeline"]] == ["Warm-up", "Breakouts"]
    assert t["timeline"][1]["t"] == pytest.approx(200.0 + OFFSET, abs=0.1)
    assert t["events"][0]["speaker_id"] == "2" and t["class"]["id"] == 900001
    assert any(a["name"] == "Cara Example" and a["absent"] for a in t["attendance"])
    assert "um" not in t["utterances"][0]["text"] and "um" in t["utterances"][0]["text_verbatim"]

    names = sorted(p.name for p in result["files"])
    base = "SYN101_2099-01-05_class900001_names"
    assert names == sorted([f"{base}.{e}" for e in ("json", "csv", "md", "html", "jsonl")]
                           + [f"{base}_debug.html", "SYN101_2099-01-05_class900001_asr.json"])
    for p in result["files"]:
        assert "SIGSECRET" not in p.read_text(encoding="utf-8")


def test_professor_without_voice_events_is_found_by_voice(tmp_path):
    """The real pattern: Forum logs no professor mic, and the professor is not on the roster.
    Their id comes from the section listing (teacher-user-id)."""
    bundle, words = scenario()
    bundle["events"] = [e for e in bundle["events"]
                        if not (e.get("event-type") == "voice" and e.get("actor-id") == 1)]
    bundle["class"] = make_class(class_users=make_class()["class-users"][1:])
    bundle["listing"] = {"section_id": 77, "class": {"id": 900001, "teacher-user-id": 1}}
    result = transcribe_bundle(bundle, out_dir=tmp_path, video_url=SIGNED,
                               provider=FakeDiarizingASR(words),
                               options=TranscribeOptions(formats=("json",)))
    t = result["transcripts"]["names"]
    assert t["alignment"]["cluster_map"]["0"] == "1"
    who = [(u["speaker_id"], round(u["start"])) for u in t["utterances"]]
    assert who[:4] == [("1", 11), ("2", 43), ("3", 56), ("1", 58)]
    assert who[-1] == (None, 310)  # 4 s off-mic voice is too little to be the professor
    assert t["speakers"]["1"]["role"] == "professor" and t["speakers"]["1"]["name"] == "Professor"
    assert result["summary"]["how_mix"]["teacher-cluster"] > 0.5
    assert not any(a.get("speaker_id") == "1" for a in t["attendance"])


def test_csv_has_spec_columns_and_class_id_from_class_object(tmp_path):
    result, _ = run(tmp_path, formats=("csv",))
    path = next(p for p in result["files"] if p.suffix == ".csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert list(rows[0].keys()) == COLUMNS == ["class_id", "speaker_id", "speaker", "start",
                                               "end", "text", "confidence"]
    assert {r["class_id"] for r in rows} == {"900001"}
    assert rows[0]["speaker"] == "Prof Example" and rows[-1]["speaker"] == UNATTRIBUTED
    assert float(rows[1]["start"]) == pytest.approx(42.0 + OFFSET, abs=0.01)


def test_jsonl_rows_one_per_utterance(tmp_path):
    result, _ = run(tmp_path, formats=("jsonl",))
    path = next(p for p in result["files"] if p.suffix == ".jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    t = result["transcripts"]["names"]
    assert len(rows) == len(t["utterances"])
    assert set(rows[0]) >= {"class_id", "start", "end", "speaker_id", "is_me", "text",
                            "timeline_segment"}
    alice = next(r for r in rows if r["speaker_id"] == "2")
    assert alice["is_me"] is True and alice["class_id"] == 900001
    assert alice["timeline_segment"] == "Warm-up"
    assert rows[0]["text"].split()[3] == "um"  # JSONL keeps fillers
    late = [r for r in rows if r["start"] > 201]
    assert late and all(r["timeline_segment"] == "Breakouts" for r in late)


def test_ids_mode_has_no_names_anywhere(tmp_path):
    result, _ = run(tmp_path, privacy="ids", formats=("json", "csv", "md", "html", "jsonl"))
    t = result["transcripts"]["ids"]
    keys = set(t["speakers"])
    assert all(re.fullmatch(r"u_[0-9a-f]{10}", k) for k in keys)
    assert sum(1 for s in t["speakers"].values() if s["is_me"]) == 1
    for p in result["files"]:
        text = p.read_text(encoding="utf-8")
        for name in ("Alice", "Bob Example", "Prof Example", "Cara", "Chatty"):
            if p.name.endswith("_asr.json"):
                continue
            assert name not in text, (p.name, name)


def test_html_is_self_contained_and_escaped(tmp_path):
    result, _ = run(tmp_path, formats=("html",))
    html = next(p for p in result["files"] if p.name.endswith("_names.html")).read_text()
    assert "<script src" not in html and "<link" not in html and "@import" not in html
    assert not re.search(r"""(src|href)=["']https?://""", html.replace(
        'href="https://forum.minerva.edu', ""))
    assert "&lt;b&gt;please&lt;/b&gt;" in html and "<b>please</b>" not in html
    assert 'id="q"' in html and "Unattributed" in html and "Breakouts" in html
    debug = next(p for p in result["files"] if p.name.endswith("_debug.html")).read_text()
    for lane in ("1. Forum voice events (raw)", "2. Forum voice events (calibrated)",
                 "3. Deepgram clusters", "4. Final turns + words by how"):
        assert lane in debug


def test_markdown_lists_chapters_and_speakers(tmp_path):
    result, _ = run(tmp_path, formats=("md",))
    md = next(p for p in result["files"] if p.suffix == ".md").read_text()
    assert md.startswith("# Synthetic Seminar: Testing")
    assert "### [0:00] Warm-up" in md and "**[0:10] Prof Example:**" in md


def test_pdf_export(tmp_path):
    pytest.importorskip("reportlab")
    result, _ = run(tmp_path, formats=("pdf",))
    pdf = next(p for p in result["files"] if p.suffix == ".pdf")
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_both_privacy_modes_and_expired_url(tmp_path):
    result, _ = run(tmp_path, privacy="both", formats=("json",), debug_html=False,
                    save_asr=False)
    assert sorted(result["transcripts"]) == ["ids", "names"]
    assert len(result["files"]) == 2
    bundle, words = scenario()
    expired = "https://dexample.cloudfront.net/u/rec.mp4?Expires=946684800&Signature=X"
    with pytest.raises(PipelineError, match="expired"):
        transcribe_bundle(bundle, out_dir=tmp_path, video_url=expired,
                          provider=FakeDiarizingASR(words), options=TranscribeOptions())


def test_forum_only_provider_warns_and_still_attributes(tmp_path):
    bundle, words = scenario()
    for w in words:
        w["cluster"] = None

    class FakeWhisper(FakeDiarizingASR):
        name = "fakewhisper"
        diarizes = False
        accepts_url = False

    asr = FakeWhisper(words)
    audio = tmp_path / "in.ogg"
    audio.write_bytes(b"x")
    import scribe_core.pipeline as pl

    orig = pl.to_opus
    pl.to_opus = lambda src, out, clean=False: audio
    try:
        result = transcribe_bundle(bundle, out_dir=tmp_path / "o", audio_path=audio,
                                   provider=asr, options=TranscribeOptions(formats=("json",)))
    finally:
        pl.to_opus = orig
    s = result["summary"]
    assert any("no diarization" in w for w in s["warnings"])
    assert "forum+cluster" not in s["how_mix"] and s["how_mix"]["forum"] > 0.8


def test_parse_formats_validates():
    assert parse_formats("json, csv,json") == ["json", "csv"]
    with pytest.raises(ValueError):
        parse_formats("docx")


def test_fake_response_helper_sanity():
    assert FakeResponse(204, {}).ok


def test_own_hand_raise_log_and_staff_analytics_land_on_the_timeline(tmp_path):
    bundle, words = scenario()
    bundle["analytics"] = {"user_engagement": [
        {"user_id": 2, "hand_raises": 3, "reactions": 1, "chat_messages": 2}]}
    hand_log = [{"t": T0_MS + 41_500, "classId": "900001"},
                {"t": T0_MS + 41_600, "classId": "123"},          # another class: ignored
                {"t": T0_MS - 600_000, "classId": "900001"}]      # before recording: dropped
    result = transcribe_bundle(bundle, out_dir=tmp_path, video_url=SIGNED,
                               provider=FakeDiarizingASR(words),
                               options=TranscribeOptions(formats=("json",), debug_html=False),
                               hand_log=hand_log)
    t = result["transcripts"]["names"]
    hands = [e for e in t["events"] if e["type"] == "hand-raise-key"]
    assert len(hands) == 1 and hands[0]["speaker_id"] == "2"
    assert hands[0]["t"] == pytest.approx(41.5 + OFFSET, abs=0.1)
    assert t["participation"] == {"2": {"hand_raises": 3, "reactions": 1, "chat_messages": 2}}
    assert t["alignment"]["main_room_method"] == "most-common-voice-room"
