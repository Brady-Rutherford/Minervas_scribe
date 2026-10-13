"""Pure parsers for Forum class data: recording timeline, voice intervals, chapters, events.

This is the ONE canonical parser (v2 had three copy-pasted ``parse_forum_data``; plan bug B7).

Verified facts (live checks + Forum's own frontend bundles, Oct 2026):

* KEY CASING is header-driven: a plain fetch returns kebab-case keys (``actor-id``,
  ``event-type``, ``event-data``, ``room-signature``...), the Forum web app gets snake_case
  (``actor_id``, ``event_type``...). Everything is normalised to kebab-case first
  (``kebab``), recursively, including ``event-data``.
* ``start-time`` / ``end-time`` are whole-second ISO strings: only a fallback.
* ``timestamp`` means different things per event type (``TIMESTAMP_MEANS``):
  ``voice`` -> epoch ms of the END of the utterance (``event-data.duration`` is ms, so
  ``start_ms = timestamp - duration``); ``message`` / ``timeline-segment`` -> the START
  (equal to ``start-time``, but with ms). Older (2023) classes carry whole-second voice
  timestamps (``...324000``): parsed the same way, flagged ``precise=False``.
* ``recording-state`` is "NOREC" on every session checked, including ones with video: it is
  ignored. Every recording session with a start time is used (plan T2).
* The class recording only covers the MAIN room. Events carry ``room-signature``; the main
  room's is "Main Classroom", so that wins. Fallback heuristic (documented): the most common
  ``room-id`` among voice events, ties broken by total voice duration.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from .timeutil import epoch_ms, iso_to_ms

AUTO = "auto"
MAIN_ROOM_SIGNATURE = "main classroom"
# What ``timestamp`` marks, per event type. bookmark-note is assumed to be a point in time.
TIMESTAMP_MEANS = {"voice": "end", "message": "start", "timeline-segment": "start",
                   "bookmark-note": "start"}


def kebab(obj):
    """Recursively rewrite dict keys from snake_case to kebab-case ('_' -> '-')."""
    if isinstance(obj, dict):
        return {(k.replace("_", "-") if isinstance(k, str) else k): kebab(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [kebab(v) for v in obj]
    return obj


def normalize_events(events) -> list[dict]:
    return [e for e in kebab(events or []) if isinstance(e, dict)]


@dataclass(frozen=True)
class RecordingSegment:
    """One recording session placed on the video timeline (assumed concatenated in order)."""

    wall_start_ms: int
    wall_end_ms: int | None  # None = open-ended (last session, no end reported)
    video_start: float
    session_id: object = None

    def contains(self, t_ms: float) -> bool:
        return t_ms >= self.wall_start_ms and (self.wall_end_ms is None or t_ms <= self.wall_end_ms)

    @property
    def video_end(self) -> float | None:
        if self.wall_end_ms is None:
            return None
        return self.video_start + (self.wall_end_ms - self.wall_start_ms) / 1000.0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- timeline


def _sessions(klass) -> list[dict]:
    k = kebab(klass or {})
    return [rs for rs in k.get("recording-sessions") or [] if isinstance(rs, dict)]


def recording_timeline(klass: dict | None) -> list[RecordingSegment]:
    """Map Forum wall-clock time to seconds into the video, across ALL recording sessions.

    Assumes the downloadable video is the sessions concatenated in order. That is unverified
    for classes with a restart; calibration (calibrate.py) detects a jump if it's wrong.
    """
    rows = []
    for rs in _sessions(klass):
        a = iso_to_ms(rs.get("recording-started"))
        if a is None:
            continue
        rows.append((a, iso_to_ms(rs.get("recording-ended")), rs.get("id")))
    rows.sort(key=lambda r: r[0])
    segs: list[RecordingSegment] = []
    video = 0.0
    for idx, (a, b, sid) in enumerate(rows):
        last = idx == len(rows) - 1
        if segs and segs[-1].wall_end_ms is not None and a < segs[-1].wall_end_ms:
            a = segs[-1].wall_end_ms  # overlapping sessions: never count time twice
        if b is not None and b <= a:
            continue
        if b is None and not last:
            continue  # a middle session with no end can't be placed
        segs.append(RecordingSegment(a, b, video, sid))
        if b is not None:
            video += (b - a) / 1000.0
    return segs


def timeline_from(source) -> list[RecordingSegment]:
    """Accept a timeline, a class dict, or a single ``recording-started`` ISO string."""
    if isinstance(source, list):
        return source
    if isinstance(source, dict):
        return recording_timeline(source)
    if isinstance(source, str):
        start = iso_to_ms(source)
        return [RecordingSegment(start, None, 0.0)] if start is not None else []
    return []


def wall_to_video(t_ms: float, segs: list[RecordingSegment]) -> float | None:
    """Wall-clock epoch ms -> video seconds, or None if it happened while not recording."""
    for s in segs:
        if s.contains(t_ms):
            return s.video_start + (t_ms - s.wall_start_ms) / 1000.0
    return None


def wall_to_video_clamped(t_ms: float, segs: list[RecordingSegment]) -> float | None:
    """Like wall_to_video, but times before / between sessions snap to the next session start
    (useful for chapter markers that began just before recording started)."""
    v = wall_to_video(t_ms, segs)
    if v is not None or not segs:
        return v
    for s in segs:
        if t_ms < s.wall_start_ms:
            return s.video_start
    return None


def wall_interval_to_video(start_ms: float, end_ms: float, segs) -> list[tuple[float, float]]:
    """Clip a wall-clock interval to the recorded spans; returns video-time pieces."""
    out = []
    for s in segs:
        a = max(start_ms, s.wall_start_ms)
        b = end_ms if s.wall_end_ms is None else min(end_ms, s.wall_end_ms)
        if b > a:
            out.append(
                (s.video_start + (a - s.wall_start_ms) / 1000.0,
                 s.video_start + (b - s.wall_start_ms) / 1000.0)
            )
    return out


def timeline_duration(segs: list[RecordingSegment]) -> float | None:
    if not segs or segs[-1].video_end is None:
        return None
    return segs[-1].video_end


# --------------------------------------------------------------------------- rooms


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def room_key(ev: dict) -> str | None:
    """``room-id`` as a string; falls back to the room signature when there is no id."""
    if ev.get("room-id") is not None:
        return str(ev["room-id"])
    sig = str(ev.get("room-signature") or "").strip()
    return f"sig:{sig}" if sig else None


def main_room_info(events) -> tuple[str | None, str | None]:
    """(main room key, method). method: "room-signature" | "most-common-voice-room" | None."""
    evs = normalize_events(events)
    sig = Counter(room_key(ev) for ev in evs
                  if str(ev.get("room-signature") or "").strip().lower() == MAIN_ROOM_SIGNATURE
                  and room_key(ev) is not None)
    if sig:
        return sig.most_common(1)[0][0], "room-signature"
    counts: Counter[str] = Counter()
    durations: dict[str, float] = defaultdict(float)
    for ev in evs:
        key = room_key(ev)
        if ev.get("event-type") != "voice" or key is None:
            continue
        counts[key] += 1
        data = ev.get("event-data") if isinstance(ev.get("event-data"), dict) else {}
        durations[key] += _num(data.get("duration")) or 0.0
    if not counts:
        return None, None
    return max(counts, key=lambda r: (counts[r], durations[r])), "most-common-voice-room"


def main_room_id(events) -> str | None:
    return main_room_info(events)[0]


# --------------------------------------------------------------------------- voice


def event_user_id(ev: dict) -> str | None:
    """``actor-id`` is always present on events; fall back to the actor object."""
    uid = ev.get("actor-id")
    if uid is None:
        actor = ev.get("actor") if isinstance(ev.get("actor"), dict) else {}
        uid = actor.get("id", actor.get("user-id"))
    return None if uid is None else str(uid)


def teacher_id(bundle: dict) -> str | None:
    """The class's ``teacher-user-id``, or None. The class_grader record doesn't carry it; the
    live class record (``classroom``) and the section listing (``listing.class``) do."""
    listing = bundle.get("listing") if isinstance(bundle.get("listing"), dict) else {}
    for part in (bundle.get("classroom"), listing.get("class"), bundle.get("class")):
        k = kebab(part) if isinstance(part, dict) else {}
        section = k.get("section") if isinstance(k.get("section"), dict) else {}
        uid = k.get("teacher-user-id", section.get("teacher-user-id"))
        if uid is not None:
            return str(uid)
    return None


def actor_name(ev: dict) -> str | None:
    """Read a display name from the event's actor object, defensively."""
    actor = ev.get("actor")
    if not isinstance(actor, dict):
        return None
    user = actor.get("user") if isinstance(actor.get("user"), dict) else actor
    parts = [user.get("first-name") or user.get("preferred-name"), user.get("last-name")]
    name = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    return name or None


def voice_wall_ms(ev: dict) -> tuple[int, int, bool] | None:
    """(start_ms, end_ms, precise) for a (kebab-case) voice event.

    ``precise`` is False when only whole-second information exists (no timestamp, or an
    older class whose timestamps are whole seconds).
    """
    data = ev.get("event-data") if isinstance(ev.get("event-data"), dict) else {}
    dur = _num(data.get("duration"))
    ts = epoch_ms(ev.get("timestamp"))  # voice: END of the utterance
    if ts is not None and dur is not None:
        return int(round(ts - dur)), ts, ts % 1000 != 0
    start_s = iso_to_ms(ev.get("start-time"))
    end_s = iso_to_ms(ev.get("end-time"))
    if ts is not None and start_s is not None:
        return start_s, ts, False
    if end_s is not None and dur is not None:
        return int(round(end_s - dur)), end_s, False
    if start_s is not None and end_s is not None:
        return start_s, end_s, False
    return None


def event_start_ms(ev: dict) -> int | None:
    """When a (kebab-case) event started, honouring per-type ``timestamp`` semantics."""
    etype = ev.get("event-type")
    if etype == "voice":
        t = voice_wall_ms(ev)
        return t[0] if t else None
    if TIMESTAMP_MEANS.get(etype, "start") == "start":
        ts = epoch_ms(ev.get("timestamp"))
        if ts is not None:
            return ts
    return iso_to_ms(ev.get("start-time"))


def parse_voice_events(
    events: list[dict],
    timeline,
    *,
    room_id=AUTO,
    include_names: bool = False,
    min_duration_ms: float = 0.0,
) -> list[dict]:
    """Voice events -> video-time intervals, sorted by start.

    ``timeline``: list of RecordingSegment, a class dict, or a ``recording-started`` string.
    ``room_id``: AUTO = main room (signature, else heuristic), None = keep all rooms, else
    that room key only. No 1 s minimum any more (plan T4). Names are only read when
    ``include_names`` (privacy mode "names").
    """
    evs = normalize_events(events)
    segs = timeline_from(timeline)
    room = main_room_id(evs) if room_id == AUTO else (None if room_id is None else str(room_id))
    out = []
    for ev in evs:
        if ev.get("event-type") != "voice":
            continue
        if room is not None and room_key(ev) != room:
            continue
        timing = voice_wall_ms(ev)
        if timing is None:
            continue
        s_ms, e_ms, precise = timing
        if e_ms - s_ms < min_duration_ms or e_ms <= s_ms:
            continue
        uid = event_user_id(ev)
        for vs, ve in wall_interval_to_video(s_ms, e_ms, segs):
            row = {
                "start": vs,
                "end": ve,
                "start_ms": s_ms,
                "end_ms": e_ms,
                "duration_ms": e_ms - s_ms,
                "user_id": uid,
                "room_id": room_key(ev),
                "event_id": ev.get("id"),
                "precise": precise,
            }
            if include_names:
                row["name"] = actor_name(ev)
            out.append(row)
    out.sort(key=lambda v: (v["start"], v["end"]))
    return out


def timing_precision(voice: list[dict]) -> str | None:
    """"ms" when most voice intervals have millisecond timing, "s" for older classes."""
    if not voice:
        return None
    return "ms" if sum(1 for v in voice if v.get("precise")) * 2 >= len(voice) else "s"


def voice_stats(events: list[dict]) -> dict:
    """Per-room and per-user voice seconds (wall clock; independent of recording)."""
    per_room: dict[str, float] = defaultdict(float)
    per_user_room: dict[tuple[str, str], float] = defaultdict(float)
    for ev in normalize_events(events):
        if ev.get("event-type") != "voice":
            continue
        t = voice_wall_ms(ev)
        if t is None:
            continue
        secs = (t[1] - t[0]) / 1000.0
        room = str(room_key(ev))
        per_room[room] += secs
        per_user_room[(event_user_id(ev) or "?", room)] += secs
    return {"per_room": dict(per_room), "per_user_room": dict(per_user_room)}


# --------------------------------------------------------------------------- chapters / events


def timeline_segments(events: list[dict], timeline) -> list[dict]:
    """``timeline-segment`` events -> chapter markers on the video timeline."""
    segs = timeline_from(timeline)
    rows = []
    for ev in normalize_events(events):
        if ev.get("event-type") != "timeline-segment":
            continue
        t_ms = event_start_ms(ev)  # timestamp = START for timeline segments
        if t_ms is None:
            continue
        t = wall_to_video_clamped(t_ms, segs)
        if t is None:
            continue
        data = ev.get("event-data") if isinstance(ev.get("event-data"), dict) else {}
        rows.append(
            {
                "t": round(t, 3),
                "title": str(data.get("timeline-segment-title") or "").strip(),
                "section": str(data.get("timeline-section-title") or "").strip(),
                "segment_id": data.get("timeline-segment-id"),
                "wall_ms": t_ms,
            }
        )
    rows.sort(key=lambda r: r["t"])
    for i, r in enumerate(rows):
        r["end"] = rows[i + 1]["t"] if i + 1 < len(rows) else None
    return rows


_TEXT_KEYS = ("message-text", "message", "text", "note", "content", "body")


def other_events(events: list[dict], timeline, *, include_private: bool = False) -> list[dict]:
    """Everything except voice / timeline-segment (``message`` chat, ``bookmark-note``...).

    Student accounts see ``voice``, ``message``, ``timeline-segment`` (and possibly
    ``bookmark-note``); every other type is optional. Private events are skipped unless
    ``include_private``.
    """
    segs = timeline_from(timeline)
    rows = []
    for ev in normalize_events(events):
        etype = ev.get("event-type")
        if etype in ("voice", "timeline-segment") or not etype:
            continue
        if ev.get("private") and not include_private:
            continue
        t_ms = event_start_ms(ev)
        t = wall_to_video(t_ms, segs) if t_ms is not None else None
        data = ev.get("event-data") if isinstance(ev.get("event-data"), dict) else {}
        text = next((data[k] for k in _TEXT_KEYS if isinstance(data.get(k), str)), None)
        rows.append(
            {
                "t": None if t is None else round(t, 3),
                "type": str(etype),
                "user_id": event_user_id(ev),
                "room_id": room_key(ev),
                "text": text,
                "private": bool(ev.get("private")),
            }
        )
    rows.sort(key=lambda r: (r["t"] is None, r["t"] or 0.0))
    return rows


def recording_bounds(klass: dict | None) -> tuple[str | None, str | None, int]:
    """(earliest recording-started, latest recording-ended, n sessions) as raw ISO strings."""
    sessions = _sessions(klass)
    starts = [(iso_to_ms(s.get("recording-started")), s.get("recording-started")) for s in sessions]
    ends = [(iso_to_ms(s.get("recording-ended")), s.get("recording-ended")) for s in sessions]
    starts = [s for s in starts if s[0] is not None]
    ends = [e for e in ends if e[0] is not None]
    first = min(starts)[1] if starts else None
    last = max(ends)[1] if ends else None
    return first, last, len(sessions)
