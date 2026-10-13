"""Transcript schema ``scribe.transcript/v2``: everything on one timeline (plan 2c, adapted).

Every export (JSON/CSV/MD/HTML/JSONL/PDF) renders from this one object.

Speakers are keyed by Forum user id. Privacy modes:
* ``names``: key = user id, with ``name``.
* ``ids``:   key = HMAC pseudonym (``u_xxxxxxxxxx``, salted, see privacy.py) and NO names
             anywhere (speakers, attendance, events). ``is_me`` is still marked so your own
             turns can be found. Spoken names inside utterance text can't be removed.
"""

from __future__ import annotations

from collections import defaultdict

from . import __version__
from .asr_deepgram import roster
from .calibrate import timing_label
from .forum_events import kebab, recording_bounds
from .privacy import pseudonym
from .timeutil import now_iso

SCHEMA = "scribe.transcript/v2"
UNATTRIBUTED = "Unattributed"


def _term_label(course: dict) -> str | None:
    term = course.get("term")
    if isinstance(term, dict):
        return term.get("name") or term.get("title") or term.get("label") or (
            str(term.get("id")) if term.get("id") is not None else None)
    return None if term in (None, "") else str(term)


def class_block(klass: dict | None, class_id=None) -> dict:
    klass = kebab(klass or {})
    section = klass.get("section") if isinstance(klass.get("section"), dict) else {}
    course = section.get("course") if isinstance(section.get("course"), dict) else {}
    cid = klass.get("id", class_id)
    started, ended, n_sessions = recording_bounds(klass)
    return {
        "id": cid,
        "title": klass.get("title") or (f"Class {cid}" if cid is not None else "Class"),
        "type": klass.get("type"),
        "course_code": course.get("course-code"),
        "course_title": course.get("title"),
        "term": _term_label(course),
        "section_id": section.get("id"),
        "section_title": section.get("title"),
        "date": klass.get("date") or (started[:10] if started else None),
        "recording_start": started,
        "recording_end": ended,
        "n_recording_sessions": n_sessions,
        "url": f"https://forum.minerva.edu/app/classes/{cid}" if cid is not None else None,
    }


def build_transcript(
    *,
    bundle: dict,
    words: list[dict],
    turns: list[dict],
    voice: list[dict],
    chapters: list[dict],
    events: list[dict],
    calibration: dict,
    mapping: dict,
    asr_info: dict,
    privacy: str = "names",
    salt: str | None = None,
    me_id=None,
    teacher_id=None,
    main_room_id=None,
    main_room_method: str | None = None,
    timing_precision: str | None = None,
    voice_names: dict | None = None,
    breakout_seconds: dict | None = None,
    how_mix: dict | None = None,
    shaky_spans: list | None = None,
    fixups: list | None = None,
    warnings: list | None = None,
    cluster_report: list | None = None,
) -> dict:
    if privacy not in ("names", "ids"):
        raise ValueError("privacy must be 'names' or 'ids'")
    if privacy == "ids" and not salt:
        raise ValueError("ids mode needs a salt (see privacy.load_or_create_salt)")
    klass = bundle.get("class") or {}
    analytics = bundle.get("analytics")
    me = None if me_id is None else str(me_id)
    names_mode = privacy == "names"

    def key(uid):
        if uid is None:
            return None
        return str(uid) if names_mode else pseudonym(uid, salt)

    people = {r["user_id"]: r for r in roster(klass) if r["user_id"] is not None}
    if teacher_id is not None:  # the professor is never on the class roster
        people.setdefault(str(teacher_id), {"user_id": str(teacher_id), "name": "Professor",
                                            "role": "professor", "absent": False})
    talk = defaultdict(float)
    for v in voice:
        if v.get("user_id") is not None:
            talk[v["user_id"]] += v["end"] - v["start"]
    turn_count = defaultdict(int)
    turn_secs = defaultdict(float)
    word_count = defaultdict(int)
    for t in turns:
        if t["user_id"] is not None:
            turn_count[t["user_id"]] += 1
            turn_secs[t["user_id"]] += t["end"] - t["start"]
            word_count[t["user_id"]] += t["n_words"]

    uids = set(talk) | set(turn_count)
    speakers = {}
    for uid in sorted(uids, key=lambda u: (-turn_secs[u], -talk[u], u)):
        info = people.get(uid, {})
        entry = {
            "role": info.get("role"),
            "is_me": me is not None and uid == me,
            "talk_s": round(talk[uid], 2),
            "breakout_talk_s": round((breakout_seconds or {}).get(uid, 0.0), 2),
            "turns": turn_count[uid],
            "words": word_count[uid],
            "transcript_s": round(turn_secs[uid], 2),
        }
        if names_mode:
            entry["name"] = info.get("name") or (voice_names or {}).get(uid) or f"User {uid}"
        speakers[key(uid)] = entry

    utterances = [
        {
            "start": t["start"],
            "end": t["end"],
            "speaker_id": key(t["user_id"]),
            "text": t["text"],
            "text_verbatim": t["text_verbatim"],
            "confidence": t["confidence"],
            "attribution": t["attribution"],
            "n_words": t["n_words"],
            "low_conf": t["low_conf"],
        }
        for t in turns
    ]

    attendance = []
    for r in people.values():
        if (r.get("role") or "") != "student":
            continue
        row = {"speaker_id": key(r["user_id"]), "absent": r["absent"]}
        if names_mode:
            row["name"] = r["name"]
        attendance.append(row)

    ev_out = [
        {k: (key(v) if k == "user_id" else v) for k, v in e.items() if k != "room_id"}
        for e in events
    ]
    for e in ev_out:
        e["speaker_id"] = e.pop("user_id", None)

    extras = {}
    for part in ("polls", "poll_responses", "outcome_assessments", "focused_outcomes",
                 "breakouts", "analytics"):
        val = bundle.get(part)
        extras[part] = len(val) if isinstance(val, list) else (None if val is None else 1)

    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "generator": {"name": "minervas-scribe", "version": __version__},
        "privacy": privacy,
        "class": class_block(klass, bundle.get("class_id")),
        "asr": asr_info,
        "alignment": {
            "timing": timing_label(calibration),
            "calibration": calibration,
            "main_room_id": main_room_id,
            "main_room_method": main_room_method,
            "forum_timing_precision": timing_precision,
            "cluster_map": {str(c): key(u) for c, u in (mapping or {}).items()},
            "clusters": [
                {**c, "best_user": key(c.get("best_user")), "mapped_to": key(c.get("mapped_to"))}
                for c in (cluster_report or [])
            ],
            "how_mix": how_mix or {},
            "edge_s": 0.25,
            "pause_s": 1.2,
            "n_words": len(words),
        },
        "speakers": speakers,
        "timeline": [{k: c[k] for k in ("t", "end", "title", "section")} for c in chapters],
        "utterances": utterances,
        "events": ev_out,
        "attendance": attendance,
        "participation": {key(u): c for u, c in engagement_by_user(analytics).items()},
        "forum_extras": extras,
        "quality": {"shaky_spans": shaky_spans or [], "name_fixups": fixups or []},
        "warnings": warnings or [],
    }


def engagement_by_user(analytics) -> dict[str, dict]:
    """Per-user counts from ``/analytics/class/{id}`` (staff only). Shape partly verified:
    ``user_engagement`` holds per-user ``hand_raises`` / ``reactions`` / ``chat_messages``.
    Accepts a list of rows or a dict keyed by user id."""
    a = kebab(analytics) if isinstance(analytics, dict) else {}
    ue = a.get("user-engagement")
    if isinstance(ue, dict):
        rows = [{**v, "user-id": v.get("user-id", v.get("id", k))}
                for k, v in ue.items() if isinstance(v, dict)]
    elif isinstance(ue, list):
        rows = [r for r in ue if isinstance(r, dict)]
    else:
        return {}
    out = {}
    for r in rows:
        user = r.get("user") if isinstance(r.get("user"), dict) else {}
        uid = r.get("user-id", r.get("id", user.get("id")))
        if uid is None:
            continue
        counts = {k.replace("-", "_"): r[k] for k in ("hand-raises", "reactions", "chat-messages")
                  if isinstance(r.get(k), int | float)}
        if counts:
            out[str(uid)] = counts
    return out


def speaker_label(transcript: dict, speaker_id) -> str:
    if speaker_id is None:
        return UNATTRIBUTED
    sp = transcript.get("speakers", {}).get(speaker_id) or {}
    return sp.get("name") or str(speaker_id)


def chapter_at(transcript: dict, t: float) -> str | None:
    title = None
    for c in transcript.get("timeline") or []:
        if c["t"] <= t:
            title = c["title"] or c.get("section") or title
        else:
            break
    return title
