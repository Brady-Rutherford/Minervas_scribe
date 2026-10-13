"""Forum API client for the local CLI, plus the event census.

Auth: the full ``Cookie`` request header string from ``FORUM_COOKIE`` (load it from a
git-ignored ``.env``). The cookie is only ever sent to https://forum.minerva.edu: redirects
are not followed and ``next`` links pointing elsewhere are refused.

The browser extension does NOT use this module: it fetches the same endpoints same-origin
from the content script, so cookies never leave the browser.

Endpoints (verified live with a student account, Oct 2026):

    GET /api/v1/users/self?show_inactive=false
    GET /api/v1/sections?hide-defaults=true                (current sections)
    GET /api/v1/sections?state=past&hide-defaults=true     (past sections)
    GET /api/v1/paginated-classes?category=past&page_size=50&filter_section_id={sid}
        (ONLY filter_section_id filters; without it you get ~12k org-wide classes)
    GET /api/v1/class_grader/classes/{cid}
    GET /api/v1/class_grader/classes/{cid}/class-events
    GET /api/v1/class_grader/classes/{cid}/polls | poll-responses | outcome-assessments
        | focused-outcomes
    GET /api/v1/class_grader/classes/{cid}/breakouts        (breakout groups; stored raw)
    GET /api/v1/recording-sessions/{CLASS_ID}/generate_recording_urls  -> {highres, lowres}
    GET /api/v1/analytics/class/{cid} | analytics/section/{sid}   (403 for students; per-user
        ``user_engagement`` incl. hand_raises / reactions / chat_messages for staff)

NEVER called: ``/api/v1/class-users/{id}`` is PATCH-only (it is where pressing H in the live
classroom saves ``hand-raised``). The client refuses that path outright.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from . import __version__
from .forum_events import normalize_events
from .redact import redact_keys, strip_query
from .timeutil import now_iso

log = logging.getLogger(__name__)

FORUM_HOST = "forum.minerva.edu"
FORUM_BASE = f"https://{FORUM_HOST}"
API = "/api/v1"
COOKIE_ENV = "FORUM_COOKIE"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Write-only endpoints this client must never touch (even with GET).
FORBIDDEN_PATH = re.compile(r"^/api/v1/class-users(/|$)")
# A section has well under this many classes. More means the filter was ignored.
MAX_CLASSES_PER_SECTION = 2000

COOKIE_HELP = (
    "Refresh FORUM_COOKIE: open forum.minerva.edu while logged in, DevTools -> Network -> "
    "click any /api/ request -> copy the full `cookie` request header into .env as "
    "FORUM_COOKIE=... (it is a credential: keep it local, it expires)."
)


class ForumError(RuntimeError):
    """Any Forum API failure."""


class ForumAuthError(ForumError):
    def __init__(self, status: int | None, path: str | None, detail: str | None = None):
        self.status = status
        self.path = path
        if detail is None:
            if status == 403:
                detail = (
                    f"Forum refused access (403) to {path}. Either FORUM_COOKIE is stale or "
                    "your role can't see this endpoint."
                )
            elif status in (301, 302, 303, 307, 308):
                detail = f"Forum redirected {path} (usually to the login page)."
            else:
                detail = f"Forum rejected the session ({status}) for {path}."
        super().__init__(f"{detail} {COOKIE_HELP}")


class ForumHTTPError(ForumError):
    def __init__(self, status: int, path: str):
        self.status = status
        self.path = path
        super().__init__(f"Forum returned HTTP {status} for {path}")


class SignedUrls(dict):
    """Signed CloudFront URLs. ``repr``/``str`` never show the query string (Signature etc.)."""

    def __repr__(self) -> str:
        inner = ", ".join(
            f"{k}={strip_query(v) + '?<signed>' if isinstance(v, str) and v else v!r}"
            for k, v in self.items()
        )
        return f"SignedUrls({inner})"

    __str__ = __repr__


def normalize_cookie(raw: str | None) -> str:
    cookie = (raw or "").strip().strip("'\"")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    return cookie


class ForumClient:
    def __init__(
        self,
        cookie: str | None = None,
        *,
        session: requests.Session | None = None,
        rate_per_sec: float = 3.0,
        max_retries: int = 4,
        backoff: float = 1.0,
        timeout: float = 60.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        raw = cookie if cookie is not None else os.environ.get(COOKIE_ENV)
        self._cookie = normalize_cookie(raw)
        if not self._cookie:
            raise ForumAuthError(None, None, "FORUM_COOKIE is not set.")
        self._session = session if session is not None else requests.Session()
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._max_retries = max_retries
        self._backoff = backoff
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last = None
        self.requests_made = 0

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _url(path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            parts = urlsplit(path_or_url)
            if (parts.hostname or "").lower() != FORUM_HOST:
                raise ForumError(
                    f"Refusing to send the Forum cookie to {parts.hostname!r} (only {FORUM_HOST})."
                )
            path, query = parts.path, parts.query
        else:
            path, _, query = path_or_url.partition("?")
            path = path if path.startswith("/") else "/" + path
        if FORBIDDEN_PATH.match(path):
            raise ForumError(f"Refusing to call {path}: write-only endpoint (hand-raise state).")
        return urlunsplit(("https", FORUM_HOST, path, query, ""))

    def _throttle(self) -> None:
        if self._last is not None and self._min_interval:
            wait = self._min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _retry_delay(self, resp, attempt: int) -> float:
        ra = resp.headers.get("Retry-After") if resp is not None else None
        if ra:
            try:
                return min(float(ra), 120.0)
            except ValueError:
                pass
        return self._backoff * (2**attempt)

    def _request(self, path_or_url: str, params: dict | None = None):
        url = self._url(path_or_url)
        shown = strip_query(url).replace(FORUM_BASE, "")
        for attempt in range(self._max_retries + 1):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers={
                        "Cookie": self._cookie,
                        "Accept": "application/json",
                        "User-Agent": f"MinervasScribe/{__version__} (local CLI)",
                    },
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if attempt < self._max_retries:
                    self._sleep(self._backoff * (2**attempt))
                    continue
                raise ForumError(f"Network error calling {shown}: {type(exc).__name__}") from None
            status = resp.status_code
            if status in (401, 403) or 300 <= status < 400:
                raise ForumAuthError(status, shown)
            if status in RETRY_STATUSES and attempt < self._max_retries:
                delay = self._retry_delay(resp, attempt)
                log.info("Forum %s for %s; retrying in %.1fs", status, shown, delay)
                self._sleep(delay)
                continue
            if status >= 400:
                raise ForumHTTPError(status, shown)
            try:
                return resp.json()
            except ValueError:
                raise ForumAuthError(
                    status, shown, f"Forum returned non-JSON for {shown} (login page?)."
                ) from None
        raise ForumError(f"Gave up on {shown}")  # pragma: no cover

    def get(self, path: str, params: dict | None = None):
        return self._request(path, params)

    def try_get(self, path: str, params: dict | None = None):
        """(data, status). 403/404/405/410 -> (None, status). 401 and redirects still raise."""
        try:
            return self._request(path, params), 200
        except ForumAuthError as exc:
            if exc.status == 403:
                return None, 403
            raise
        except ForumHTTPError as exc:
            if exc.status in (400, 404, 405, 410):
                return None, exc.status
            raise

    # ------------------------------------------------------------------ endpoints

    def me(self) -> dict:
        return self.get(f"{API}/users/self", {"show_inactive": "false"})

    def sections(self, past: bool = False) -> list[dict]:
        params = {"hide-defaults": "true"}
        if past:
            params = {"state": "past", "hide-defaults": "true"}
        data = self.get(f"{API}/sections", params)
        if isinstance(data, dict):
            data = data.get("results") or []
        return list(data or [])

    def classes(
        self, section_id, *, category: str = "past", page_size: int = 50, max_pages: int = 200
    ) -> list[dict]:
        """Every class of one section, following ``next`` links.

        ``filter_section_id`` is mandatory: Forum silently ignores ``section=``,
        ``section-id=`` and ``section_id=`` and would return ~12k org-wide classes.
        """
        if section_id in (None, ""):
            raise ValueError("section_id is required (unfiltered paginated-classes is org-wide)")
        sid = str(section_id)
        url: str | None = f"{API}/paginated-classes"
        params: dict | None = {
            "category": category, "page_size": page_size, "filter_section_id": sid,
        }
        out: list[dict] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            data = self.get(url, params)
            if isinstance(data, list):
                out.extend(data)
                break
            out.extend(data.get("results") or [])
            count = data.get("count")
            if isinstance(count, int) and count > MAX_CLASSES_PER_SECTION:
                raise ForumError(
                    f"paginated-classes reported {count} classes for section {sid}; the section "
                    "filter was probably ignored. Aborting rather than crawling the whole org."
                )
            nxt = data.get("next")
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            url, params = self._ensure_filter(nxt, sid), None
        return out

    @staticmethod
    def _ensure_filter(next_url: str, sid: str) -> str:
        parts = urlsplit(next_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if query.get("filter_section_id") != sid:
            query["filter_section_id"] = sid
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def class_json(self, class_id) -> dict:
        return self.get(f"{API}/class_grader/classes/{int(class_id)}")

    def class_events(self, class_id) -> list[dict]:
        return self.get(f"{API}/class_grader/classes/{int(class_id)}/class-events")

    BUNDLE_PARTS = {
        "class": "/class_grader/classes/{cid}",
        "classroom": "/classes/{cid}",  # the live class record: has teacher-user-id
        "events": "/class_grader/classes/{cid}/class-events",
        "polls": "/class_grader/classes/{cid}/polls",
        "poll_responses": "/class_grader/classes/{cid}/poll-responses",
        "outcome_assessments": "/class_grader/classes/{cid}/outcome-assessments",
        "focused_outcomes": "/class_grader/classes/{cid}/focused-outcomes",
        "breakouts": "/class_grader/classes/{cid}/breakouts",
        "analytics": "/analytics/class/{cid}",  # staff only (403 for students)
    }

    def class_bundle(self, class_id, *, extras: bool = True) -> dict:
        """Everything Scribe needs about one class. Each part is optional (try-get) and its
        HTTP status is recorded under ``status``. Raises if Forum refuses all of them."""
        cid = int(class_id)
        bundle: dict = {
            "schema": "scribe.bundle/v1",
            "class_id": cid,
            "fetched_at": now_iso(),
            "status": {},
        }
        for key, template in self.BUNDLE_PARTS.items():
            if not extras and key not in ("class", "classroom", "events"):
                continue
            data, status = self.try_get(API + template.format(cid=cid))
            bundle[key] = data
            bundle["status"][key] = status
        if all(s == 403 for s in bundle["status"].values()):
            raise ForumAuthError(403, f"{API}/class_grader/classes/{cid}")
        return bundle

    def probe_capabilities(self, class_id=None, section_id=None) -> dict:
        """Which optional endpoints this account can read (RBAC differs by role)."""
        me = self.me()
        probes = {}
        if class_id is not None:
            cid = int(class_id)
            for key in ("outcome_assessments", "breakouts", "analytics"):
                probes[key] = API + self.BUNDLE_PARTS[key].format(cid=cid)
            probes["live_class"] = f"{API}/classes/{cid}"
        if section_id is not None:
            probes["analytics_section"] = f"{API}/analytics/section/{int(section_id)}"
        status = {k: self.try_get(p)[1] for k, p in probes.items()}
        return {"user_id": me.get("id"), "role": me.get("role"), "endpoints": status}

    def recording_urls(self, class_id) -> SignedUrls:
        """Fresh signed CloudFront URLs (``highres``, ``lowres``). Path param is the CLASS id.

        They expire: call right before use. Never log or persist them.
        """
        data = self.get(f"{API}/recording-sessions/{int(class_id)}/generate_recording_urls")
        if not isinstance(data, dict):
            data = {}
        return SignedUrls(
            {k: data.get(k) for k in ("highres", "lowres") if isinstance(data.get(k), str)}
        )


# --------------------------------------------------------------------------- census


def _type_name(value) -> str:
    if value is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(
        type(value), type(value).__name__
    )


def event_census(events: list[dict], *, include_sample: bool = False) -> dict:
    """Counts, top-level keys and ``event-data`` keys (with value types) per event type.
    Keys are reported kebab-case; ``key_styles`` records which casing Forum actually sent.

    Name/email/credential-like keys are redacted in samples; samples are opt-in because they
    contain other people's content (chat text etc.).
    """
    census: dict[str, dict] = {}
    data_types: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    keys: dict[str, set] = defaultdict(set)
    styles: dict[str, set] = defaultdict(set)
    counts: Counter[str] = Counter()
    samples: dict[str, dict] = {}
    raw = [e for e in events or [] if isinstance(e, dict)]
    for original, ev in zip(raw, normalize_events(raw), strict=True):
        etype = str(ev.get("event-type"))
        styles[etype].add("snake_case" if "event_type" in original else "kebab-case")
        counts[etype] += 1
        keys[etype].update(ev.keys())
        data = ev.get("event-data")
        if isinstance(data, dict):
            for k, v in data.items():
                data_types[etype][k].add(_type_name(v))
        if include_sample and etype not in samples:
            samples[etype] = ev
    for etype, n in counts.most_common():
        row = {
            "count": n,
            "keys": sorted(keys[etype]),
            "data_keys": {k: sorted(v) for k, v in sorted(data_types[etype].items())},
            "key_styles": sorted(styles[etype]),
        }
        # Only key names and value *types* are reported; values appear only in opt-in samples.
        if include_sample:
            row["sample"] = redact_keys(samples[etype])
        census[etype] = row
    return census


def merge_census(census_list: list[dict]) -> dict:
    """Combine per-class censuses: totals plus the number of classes each type appears in."""
    out: dict[str, dict] = {}
    for census in census_list:
        for etype, row in census.items():
            agg = out.setdefault(etype, {"count": 0, "classes": 0, "data_keys": set()})
            agg["count"] += row["count"]
            agg["classes"] += 1
            agg["data_keys"].update(row["data_keys"].keys())
    return {
        k: {**v, "data_keys": sorted(v["data_keys"])}
        for k, v in sorted(out.items(), key=lambda kv: -kv[1]["count"])
    }
