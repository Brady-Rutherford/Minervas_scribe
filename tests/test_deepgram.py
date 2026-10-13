import logging

import pytest
from conftest import FakeResponse, FakeSession, make_class

from scribe_core.asr_base import ASRError
from scribe_core.asr_deepgram import (
    DG_URL,
    DeepgramASR,
    build_keyterms,
    build_params,
    roster,
    words_from_deepgram,
)

SIGNED = ("https://dexample.cloudfront.net/uuid/zencoder-recording-600k.mp4"
          "?Expires=4102444800&Signature=SYNTHETICSIG&Key-Pair-Id=SYNTHKEY")

DG_RESPONSE = {
    "metadata": {"request_id": "req-1", "duration": 12.5},
    "results": {
        "channels": [{"alternatives": [{"words": [
            {"word": "hello", "punctuated_word": "Hello,", "start": 0.5, "end": 0.8,
             "confidence": 0.98, "speaker": 0, "speaker_confidence": 0.9},
            {"word": "um", "punctuated_word": "um", "start": 0.9, "end": 1.0,
             "confidence": 0.7, "speaker": 0, "speaker_confidence": 0.6},
            {"word": "zoltan", "punctuated_word": "Zoltan.", "start": 1.1, "end": 1.5,
             "confidence": 0.55, "speaker": 1, "speaker_confidence": 0.85},
        ]}]}],
        "utterances": [{"start": 0.5, "end": 1.5}],
    },
}


def test_roster_handles_nested_user_objects():
    r = roster(make_class())
    assert r[1] == {"user_id": "2", "first": "Alice", "last": "Example", "preferred": "",
                    "name": "Alice Example", "role": "student", "absent": False}


def test_roster_and_keyterms_handle_flat_class_users():
    klass = make_class(class_users=[
        {"user-id": 11, "first-name": "Dana", "last-name": "Flat", "role": "student"},
        {"id": 12, "first-name": "Eli", "last-name": "Flat", "preferred-name": "Eddie",
         "role": "professor"},
    ])
    r = roster(klass)
    assert [x["user_id"] for x in r] == ["11", "12"]
    assert r[1]["name"] == "Eddie Flat"
    terms = build_keyterms({"class": klass})
    assert terms[:3] == ["Dana Flat", "Eli Flat", "Eddie Flat"]


def test_keyterms_names_first_then_course_and_title_deduped():
    klass = make_class()
    klass["class-users"].append({"role": "student", "user": {"id": 9, "first-name": "alice",
                                                             "last-name": "example"}})
    terms = build_keyterms({"class": klass}, extra=["Hungarian algorithm"])
    assert terms == ["Prof Example", "Alice Example", "Bob Example", "Cara Example",
                     "Synthetic Studies", "Synthetic Seminar: Testing", "Hungarian algorithm"]


def test_keyterm_budget_is_respected():
    users = [{"role": "student", "user": {"id": i, "first-name": f"Name{i}",
                                          "last-name": f"Surname{i}"}} for i in range(80)]
    terms = build_keyterms(make_class(class_users=users), budget=90)
    assert sum(len(t.split()) for t in terms) <= 90
    assert len(terms) == 45 and terms[0] == "Name0 Surname0"


def test_params_repeat_keyterm_and_opt_out():
    params = build_params(keyterms=["Alice Example", "Bob Example"], language="multi")
    d = dict(params)
    assert d["model"] == "nova-3" and d["diarize"] == "true" and d["utterances"] == "true"
    assert d["utt_split"] == "0.8" and d["smart_format"] == "true"
    assert d["mip_opt_out"] == "true" and d["language"] == "multi"
    assert [v for k, v in params if k == "keyterm"] == ["Alice Example", "Bob Example"]


def test_words_from_deepgram_keeps_clusters_and_flags_fillers():
    words = words_from_deepgram(DG_RESPONSE)
    assert words[0] == {"start": 0.5, "end": 0.8, "text": "Hello,", "conf": 0.98, "cluster": 0,
                        "cluster_conf": 0.9, "filler": False}
    assert words[1]["filler"] is True
    with pytest.raises(ASRError):
        words_from_deepgram({"results": {}})


def test_url_mode_sends_signed_url_json_to_eu_endpoint(caplog):
    session = FakeSession([FakeResponse(200, DG_RESPONSE)])
    asr = DeepgramASR(api_key="dg-synthetic", session=session)
    with caplog.at_level(logging.DEBUG):
        res = asr.transcribe(video_url=SIGNED, keyterms=["Alice Example", "Bob Example"])
    call = session.calls[0]
    assert call["url"] == DG_URL == "https://api.eu.deepgram.com/v1/listen"
    assert call["json"] == {"url": SIGNED}
    assert call["headers"]["Authorization"] == "Token dg-synthetic"
    assert [v for k, v in call["params"] if k == "keyterm"] == ["Alice Example", "Bob Example"]
    assert dict(call["params"])["mip_opt_out"] == "true"
    assert res.mode == "url" and res.diarized and res.duration == 12.5 and len(res.words) == 3
    assert "SYNTHETICSIG" not in caplog.text and "Expires" not in caplog.text


def test_url_mode_failure_falls_back_to_upload(tmp_path, caplog):
    audio = tmp_path / "audio_clean.ogg"
    audio.write_bytes(b"OggS-synthetic")
    session = FakeSession([
        FakeResponse(400, {"err_msg": f"could not fetch {SIGNED}"}),
        FakeResponse(200, DG_RESPONSE),
    ])
    calls = []

    def fallback():
        calls.append(1)
        return audio

    asr = DeepgramASR(api_key="dg-synthetic", session=session)
    with caplog.at_level(logging.DEBUG):
        res = asr.transcribe(video_url=SIGNED, audio_fallback=fallback)
    assert calls == [1]
    assert res.mode == "upload"
    upload = session.calls[1]
    assert upload["headers"]["Content-Type"] == "audio/ogg"
    assert "json" not in upload and upload["data"] is not None
    assert "SYNTHETICSIG" not in caplog.text
    assert "SYNTHETICSIG" not in (res.meta["url_mode_error"] or "")


def test_bad_api_key_does_not_fall_back():
    session = FakeSession([FakeResponse(401, {"err_msg": "bad key"})])
    asr = DeepgramASR(api_key="nope", session=session)
    with pytest.raises(ASRError, match="API key"):
        asr.transcribe(video_url=SIGNED, audio_fallback=lambda: pytest.fail("no upload"))


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(ASRError, match="DEEPGRAM_API_KEY"):
        DeepgramASR(session=FakeSession([])).transcribe(video_url=SIGNED)
