import numpy as np
import pytest
from conftest import words_from_intervals

from scribe_core.metrics import (
    attribution_accuracy,
    boundary_error,
    compare_configs,
    format_table,
    legacy_attribution,
    name_accuracy,
    wer,
)
from scribe_core.turns import build_turns


def w(s, e, uid, text, conf=0.9, how="forum", filler=False):
    return {"start": s, "end": e, "user_id": uid, "text": text, "conf": conf, "how": how,
            "filler": filler}


def test_turns_split_on_speaker_change_and_long_pause():
    words = [
        w(0.0, 0.4, "A", "Hello"), w(0.5, 0.9, "A", "everyone."),
        w(1.0, 1.3, "B", "Hi!"),
        w(1.4, 1.7, "B", "um", filler=True), w(1.8, 2.2, "B", "question?"),
        w(4.0, 4.4, "B", "Also"),                     # 1.8 s pause > 1.2: new turn
        w(4.5, 4.9, None, "mumble", conf=0.3, how="none"),
    ]
    turns = build_turns(words)
    assert [(t["user_id"], t["text"]) for t in turns] == [
        ("A", "Hello everyone."), ("B", "Hi! question?"), ("B", "Also"), (None, "mumble")]
    assert turns[1]["text_verbatim"] == "Hi! um question?"
    assert turns[3]["low_conf"] == [[0, 6]]
    assert turns[0]["attribution"] == {"forum": 1.0}
    assert turns[0]["confidence"] == pytest.approx(0.9)


def test_low_conf_spans_point_into_clean_text():
    words = [w(0, 0.2, "A", "We"), w(0.3, 0.5, "A", "discussed", conf=0.4),
             w(0.6, 0.8, "A", ","), w(0.9, 1.2, "A", "Zoltan", conf=0.5)]
    t = build_turns(words)[0]
    assert t["text"] == "We discussed, Zoltan"
    assert [t["text"][a:b] for a, b in t["low_conf"]] == ["discussed", "Zoltan"]


def test_attribution_accuracy_counts_seconds():
    gold = [{"start": 0, "end": 10, "user_id": "A"}, {"start": 10, "end": 20, "user_id": "B"}]
    pred = [{"start": 0, "end": 12, "user_id": "A"}, {"start": 12, "end": 20, "user_id": "B"}]
    assert attribution_accuracy(pred, gold) == pytest.approx(18 / 20)
    pred_none = [{"start": 0, "end": 20, "user_id": None}]
    assert attribution_accuracy(pred_none, gold) == 0.0
    assert attribution_accuracy(pred, []) == 0.0


def test_boundary_error_is_median_nearest_start():
    gold = [{"start": 0.0}, {"start": 10.0}, {"start": 20.0}]
    pred = [{"start": 0.1}, {"start": 10.5}, {"start": 19.7}, {"start": 30.0}]
    assert boundary_error(pred, gold) == pytest.approx(0.3)
    assert boundary_error([], gold) is None


def test_wer_and_name_accuracy():
    assert wer("the cat sat", "the cat sat") == 0.0
    assert wer("the cat sat", "a cat sat down") == pytest.approx(2 / 3)
    assert name_accuracy(["Zoltan Example"], "thanks zoltan example") == 1.0


def test_legacy_v2_attribution_credits_fallback_and_drops_short_events():
    voice = [{"start": 0, "end": 0.5, "user_id": "B"}, {"start": 1, "end": 5, "user_id": "A"}]
    segs = [{"start": 0.2, "end": 3, "text": "x"}, {"start": 2, "end": 9, "text": "y"}]
    out = legacy_attribution(segs, voice, fallback_user="PROF")
    assert [s["user_id"] for s in out] == ["PROF", "A"]


def test_compare_configs_runs_and_full_pipeline_wins_with_offset():
    rng = np.random.default_rng(2)
    t, gold, speech = 1.0, [], []
    users = ["A", "B", "C"]
    while t < 900:
        uid = users[int(rng.integers(0, 3))]
        length = float(rng.uniform(1.0, 8.0))
        gold.append({"start": t, "end": t + length, "user_id": uid})
        speech.append((t, t + length, users.index(uid)))
        t += length + float(rng.uniform(0.3, 2.0))
    words = words_from_intervals(speech)
    offset = 1.5  # Forum clock 1.5 s behind the audio
    voice_raw = [{"start": g["start"] - offset, "end": g["end"] - offset,
                  "user_id": g["user_id"]} for g in gold]
    rows = compare_configs(words, voice_raw, gold, dur=905)
    by = {r["config"]: r for r in rows}
    full = by["full (calibrate+fuse)"]
    assert full["attribution_accuracy"] > 0.9
    assert full["attribution_accuracy"] > by["v2 baseline"]["attribution_accuracy"]
    assert "offset=+1.50s" in full["timing"]
    table = format_table(rows)
    assert table.startswith("| config |") and "deepgram-only" in table
