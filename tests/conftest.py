"""Synthetic fixtures only: no real class data, no real people, no credentials."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

T0 = datetime(2099, 1, 5, 15, 0, 0, tzinfo=UTC)  # synthetic recording start
T0_MS = int(T0.timestamp() * 1000)
MAIN_ROOM = "room-main"


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def iso_ms(ms: int) -> str:
    return iso(datetime.fromtimestamp(ms / 1000, tz=UTC))


def voice_event(uid, start_ms: int, dur_ms: int, room=MAIN_ROOM, eid=None, name=None) -> dict:
    """A Forum-shaped voice event. start-time/end-time are whole seconds (as in Forum);
    ``timestamp`` is epoch ms of the END and ``event-data.duration`` is ms."""
    end_ms = start_ms + dur_ms
    first, _, last = (name or f"Person{uid} Example").partition(" ")
    return {
        "id": eid or f"ev-{uid}-{start_ms}",
        "actor-id": uid,
        "actor": {"id": uid, "first-name": first, "last-name": last},
        "created-on": iso_ms(end_ms),
        "start-time": iso(datetime.fromtimestamp(start_ms // 1000, tz=UTC)),
        "end-time": iso(datetime.fromtimestamp(end_ms // 1000, tz=UTC)),
        "timestamp": end_ms,
        "event-type": "voice",
        "event-data": {"duration": dur_ms},
        "klass-id": 900001,
        "private": False,
        "room-id": room,
        "room-signature": "synthetic-signature-should-be-redacted",
    }


def timeline_event(title: str, at_ms: int, seg_id=1) -> dict:
    return {
        "id": f"tl-{seg_id}",
        "actor-id": 1,
        "start-time": iso_ms(at_ms),
        "timestamp": at_ms,
        "event-type": "timeline-segment",
        "event-data": {"timeline-segment-id": seg_id, "timeline-segment-title": title},
        "room-id": MAIN_ROOM,
    }


def chat_event(uid, at_ms: int, text: str, private=False) -> dict:
    return {
        "id": f"msg-{uid}-{at_ms}",
        "actor-id": uid,
        "actor": {"id": uid, "first-name": "Chatty", "last-name": "Example"},
        "timestamp": at_ms,
        "start-time": iso_ms(at_ms),
        "event-type": "message",
        "event-data": {"message": text},
        "room-id": MAIN_ROOM,
        "private": private,
    }


def make_class(sessions=None, class_users=None) -> dict:
    if sessions is None:
        sessions = [(T0, T0 + timedelta(minutes=90))]
    return {
        "id": 900001,
        "title": "Synthetic Seminar: Testing",
        "type": "seminar",
        "assessment": None,
        "section": {
            "id": 77,
            "title": "SYN101 Section A",
            "course": {"id": 5, "course-code": "SYN101", "title": "Synthetic Studies",
                       "term": {"name": "Spring 2099"}},
        },
        "class-users": class_users if class_users is not None else [
            {"role": "professor", "user": {"id": 1, "first-name": "Prof", "last-name": "Example"}},
            {"role": "student", "absent": False,
             "user": {"id": 2, "first-name": "Alice", "last-name": "Example"}},
            {"role": "student", "absent": False,
             "user": {"id": 3, "first-name": "Bob", "last-name": "Example"}},
            {"role": "student", "absent": True,
             "user": {"id": 4, "first-name": "Cara", "last-name": "Example"}},
        ],
        "recording-sessions": [
            {"id": 500 + i, "klass-id": 900001, "recording-started": iso(a),
             "recording-ended": iso(b), "recording-state": "NOREC", "recording-uuid": f"uuid-{i}"}
            for i, (a, b) in enumerate(sessions)
        ],
    }


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)
        self.ok = 200 <= status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records requests; answers from a list of responses or a callable(url, params)."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []

    def _answer(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if callable(self.responder):
            return self.responder(method, url, kwargs)
        return self.responder.pop(0)

    def get(self, url, **kwargs):
        return self._answer("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, **kwargs)


@pytest.fixture
def klass():
    return make_class()


def words_from_intervals(intervals, word_s=0.3, gap_s=0.05, cluster=None, conf=0.95):
    """Split (start, end[, cluster]) speech intervals into word dicts."""
    out = []
    for iv in intervals:
        s, e = iv[0], iv[1]
        cl = iv[2] if len(iv) > 2 else cluster
        t = s
        i = 0
        while t + 0.05 < e:
            we = min(e, t + word_s)
            out.append({"start": round(t, 3), "end": round(we, 3), "text": f"w{i}",
                        "conf": conf, "cluster": cl, "cluster_conf": 0.9, "filler": False})
            t = we + gap_s
            i += 1
    return out
