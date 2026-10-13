from datetime import timedelta

import pytest
from conftest import (
    MAIN_ROOM,
    T0,
    T0_MS,
    chat_event,
    iso_ms,
    make_class,
    timeline_event,
    voice_event,
)

from scribe_core.forum_events import (
    TIMESTAMP_MEANS,
    event_start_ms,
    main_room_id,
    main_room_info,
    other_events,
    parse_voice_events,
    recording_timeline,
    timeline_segments,
    timing_precision,
    voice_wall_ms,
    wall_interval_to_video,
    wall_to_video,
)


def test_voice_timing_uses_timestamp_as_end_ms_and_duration():
    # Utterance from 15:00:10.250 to 15:00:12.791 (2.541 s). start-time/end-time are
    # whole seconds and would say 10 -> 12; timestamp is the precise END in ms.
    ev = voice_event(2, T0_MS + 10_250, 2_541)
    assert ev["start-time"].endswith("15:00:10Z") and ev["end-time"].endswith("15:00:12Z")
    s_ms, e_ms, precise = voice_wall_ms(ev)
    assert precise is True
    assert e_ms == T0_MS + 12_791
    assert s_ms == T0_MS + 10_250

    rows = parse_voice_events([ev], make_class())
    assert len(rows) == 1
    assert rows[0]["start"] == pytest.approx(10.250, abs=1e-6)  # video-relative seconds
    assert rows[0]["end"] == pytest.approx(12.791, abs=1e-6)
    assert rows[0]["user_id"] == "2"
    assert rows[0]["room_id"] == MAIN_ROOM


def test_voice_timing_accepts_recording_started_string_and_numeric_string_timestamp():
    ev = voice_event(2, T0_MS + 5_000, 1_000)
    ev["timestamp"] = str(ev["timestamp"])
    rows = parse_voice_events([ev], "2099-01-05T15:00:00Z")
    assert rows[0]["start"] == pytest.approx(5.0)
    assert rows[0]["end"] == pytest.approx(6.0)


def test_voice_timing_falls_back_to_whole_seconds_without_timestamp():
    ev = voice_event(2, T0_MS + 5_000, 2_000)
    del ev["timestamp"]
    s_ms, e_ms, precise = voice_wall_ms(ev)
    assert precise is False
    assert (s_ms, e_ms) == (T0_MS + 5_000, T0_MS + 7_000)


def test_short_events_are_kept_and_names_only_on_request():
    ev = voice_event(3, T0_MS + 1_000, 330, name="Bob Example")  # "yeah" (T4)
    rows = parse_voice_events([ev], make_class())
    assert len(rows) == 1 and "name" not in rows[0]
    rows = parse_voice_events([ev], make_class(), include_names=True)
    assert rows[0]["name"] == "Bob Example"


def test_main_room_is_most_common_and_breakouts_are_dropped():
    events = [voice_event(1, T0_MS + i * 10_000, 3_000) for i in range(6)]
    events += [voice_event(2, T0_MS + 1_000, 2_000, room="breakout-1"),
               voice_event(3, T0_MS + 2_000, 2_000, room="breakout-2"),
               voice_event(2, T0_MS + 4_000, 2_000, room="breakout-1")]
    assert main_room_id(events) == MAIN_ROOM
    rows = parse_voice_events(events, make_class())
    assert {r["room_id"] for r in rows} == {MAIN_ROOM}
    assert len(rows) == 6
    assert len(parse_voice_events(events, make_class(), room_id=None)) == 9


def test_main_room_tie_breaks_on_duration():
    events = [voice_event(1, T0_MS, 5_000, room="a"), voice_event(2, T0_MS, 1_000, room="b")]
    assert main_room_id(events) == "a"
    assert main_room_id([]) is None


def test_two_recording_sessions_map_wall_clock_to_concatenated_video():
    # Session 1: 15:00-15:30 (1800 s of video); gap; session 2: 15:40-16:10.
    klass = make_class(sessions=[
        (T0, T0 + timedelta(minutes=30)),
        (T0 + timedelta(minutes=40), T0 + timedelta(minutes=70)),
    ])
    segs = recording_timeline(klass)
    assert [s.video_start for s in segs] == [0.0, 1800.0]
    assert wall_to_video(T0_MS + 60_000, segs) == pytest.approx(60.0)
    assert wall_to_video(T0_MS + 35 * 60_000, segs) is None  # not recording: dropped
    assert wall_to_video(T0_MS + 45 * 60_000, segs) == pytest.approx(1800.0 + 300.0)

    in_gap = voice_event(2, T0_MS + 35 * 60_000, 2_000)
    after = voice_event(2, T0_MS + 41 * 60_000, 2_000)
    rows = parse_voice_events([in_gap, after], klass)
    assert len(rows) == 1
    assert rows[0]["start"] == pytest.approx(1800.0 + 60.0)


def test_interval_spanning_a_restart_is_clipped_into_pieces():
    klass = make_class(sessions=[
        (T0, T0 + timedelta(minutes=30)),
        (T0 + timedelta(minutes=40), T0 + timedelta(minutes=70)),
    ])
    segs = recording_timeline(klass)
    pieces = wall_interval_to_video(T0_MS + 29 * 60_000, T0_MS + 41 * 60_000, segs)
    assert pieces == [(pytest.approx(1740.0), pytest.approx(1800.0)),
                      (pytest.approx(1800.0), pytest.approx(1860.0))]


def test_recording_sessions_are_sorted_and_norec_state_is_ignored():
    klass = make_class(sessions=[
        (T0 + timedelta(minutes=40), T0 + timedelta(minutes=70)),
        (T0, T0 + timedelta(minutes=30)),
    ])
    assert all(rs["recording-state"] == "NOREC" for rs in klass["recording-sessions"])
    segs = recording_timeline(klass)
    assert segs[0].wall_start_ms == T0_MS
    assert segs[1].video_start == pytest.approx(1800.0)


def test_timeline_segments_become_chapters_with_ends():
    events = [timeline_event("Breakout Instructions", T0_MS + 600_000, 2),
              timeline_event("Warm-up", T0_MS - 30_000, 1),  # began before recording
              timeline_event("Breakouts", T0_MS + 900_000, 3)]
    ch = timeline_segments(events, make_class())
    assert [c["title"] for c in ch] == ["Warm-up", "Breakout Instructions", "Breakouts"]
    assert ch[0]["t"] == 0.0
    assert ch[1]["t"] == pytest.approx(600.0) and ch[0]["end"] == pytest.approx(600.0)
    assert ch[-1]["end"] is None


def test_other_events_skip_private_and_extract_chat_text():
    events = [chat_event(2, T0_MS + 120_000, "see slide 3"),
              chat_event(3, T0_MS + 130_000, "secret", private=True),
              voice_event(2, T0_MS, 1_000)]
    rows = other_events(events, make_class())
    assert len(rows) == 1
    assert rows[0] == {"t": 120.0, "type": "message", "user_id": "2", "room_id": MAIN_ROOM,
                       "text": "see slide 3", "private": False}
    assert len(other_events(events, make_class(), include_private=True)) == 2


# ----------------------------------------------------------------- facts from the frontend


def snake(obj):
    if isinstance(obj, dict):
        return {k.replace("-", "_"): snake(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [snake(v) for v in obj]
    return obj


def test_snake_case_and_kebab_case_events_parse_identically():
    events = [voice_event(2, T0_MS + 10_250, 2_541),
              voice_event(3, T0_MS + 20_000, 900, room="breakout-1"),
              timeline_event("Breakouts", T0_MS + 600_123, 3),
              chat_event(2, T0_MS + 120_000, "hello")]
    events[2]["event-data"]["timeline-section-title"] = "Activity 2"
    snaked = snake(events)
    assert "event_type" in snaked[0] and "timeline_segment_title" in snaked[2]["event_data"]
    klass = make_class()
    assert parse_voice_events(snaked, klass) == parse_voice_events(events, klass)
    assert timeline_segments(snaked, klass) == timeline_segments(events, klass)
    assert timeline_segments(snaked, klass)[0]["section"] == "Activity 2"
    assert other_events(snaked, klass) == other_events(events, klass)
    assert main_room_id(snaked) == main_room_id(events)
    # a snake_case class object (recording_sessions, recording_started...) works too
    assert parse_voice_events(snaked, snake(klass))[0]["start"] == pytest.approx(10.25)


def test_main_classroom_signature_beats_the_count_heuristic():
    main = [voice_event(1, T0_MS + i * 9_000, 2_000, room="r-main") for i in range(3)]
    side = [voice_event(2, T0_MS + i * 4_000, 2_000, room="r-b1") for i in range(8)]
    for ev in main:
        ev["room-signature"] = "Main Classroom"
    for ev in side:
        ev["room-signature"] = "Breakout Room 1"
    events = main + side
    assert main_room_info(events) == ("r-main", "room-signature")
    assert {r["room_id"] for r in parse_voice_events(events, make_class())} == {"r-main"}
    for ev in events:
        ev["room-signature"] = "synthetic"
    assert main_room_info(events) == ("r-b1", "most-common-voice-room")


def test_signature_alone_identifies_rooms_when_ids_are_missing():
    a = voice_event(1, T0_MS, 2_000)
    b = voice_event(2, T0_MS + 5_000, 2_000)
    for ev, sig in ((a, "Main Classroom"), (b, "Breakout Room 3")):
        del ev["room-id"]
        ev["room-signature"] = sig
    rows = parse_voice_events([a, b], make_class())
    assert [r["user_id"] for r in rows] == ["1"]


def test_timestamp_semantics_per_event_type():
    # voice: timestamp = END; message and timeline-segment: timestamp = START (ms precision)
    v = voice_event(2, T0_MS + 30_000, 4_321)
    assert event_start_ms(v) == T0_MS + 30_000
    m = chat_event(2, T0_MS + 61_234, "q?")
    assert event_start_ms(m) == T0_MS + 61_234
    tl = timeline_event("Poll: warm-up", T0_MS + 90_456)
    tl["start-time"] = iso_ms(T0_MS + 90_000)  # whole-second ISO string
    assert event_start_ms(tl) == T0_MS + 90_456
    assert timeline_segments([tl], make_class())[0]["t"] == pytest.approx(90.456)
    assert TIMESTAMP_MEANS["voice"] == "end" and TIMESTAMP_MEANS["message"] == "start"


def test_whole_second_voice_timestamps_from_older_classes_are_flagged():
    old = voice_event(2, T0_MS + 10_000, 3_000)  # ends on a whole second, like 2023 data
    assert old["timestamp"] % 1000 == 0
    rows = parse_voice_events([old], make_class())
    assert rows[0]["precise"] is False and rows[0]["end"] == pytest.approx(13.0)
    assert timing_precision(rows) == "s"
    new = parse_voice_events([voice_event(2, T0_MS + 10_000, 3_217)], make_class())
    assert timing_precision(new) == "ms"


def test_message_text_and_bookmark_notes():
    msg = {"event_type": "message", "actor_id": 2, "timestamp": T0_MS + 5_000,
           "event_data": {"message_id": 9, "message_text": "see the reading"}}
    note = {"event-type": "bookmark-note", "actor-id": 2, "timestamp": T0_MS + 7_000,
            "event-data": {"note": "review this"}}
    rows = other_events([msg, note], make_class())
    assert [(r["type"], r["t"], r["text"]) for r in rows] == [
        ("message", 5.0, "see the reading"), ("bookmark-note", 7.0, "review this")]
