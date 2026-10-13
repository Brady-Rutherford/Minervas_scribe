import pytest
from conftest import FakeResponse, FakeSession, voice_event

from scribe_core.forum import (
    ForumAuthError,
    ForumClient,
    ForumError,
    ForumHTTPError,
    SignedUrls,
    event_census,
    merge_census,
    normalize_cookie,
)

COOKIE = "sessionid=synthetic; csrftoken=synthetic"


def client(responder, **kw):
    session = FakeSession(responder)
    sleeps = []
    c = ForumClient(COOKIE, session=session, sleep=sleeps.append, **kw)
    return c, session, sleeps


def test_missing_cookie_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FORUM_COOKIE", raising=False)
    with pytest.raises(ForumAuthError, match="FORUM_COOKIE"):
        ForumClient()


def test_cookie_comes_from_env_and_only_goes_to_forum(monkeypatch):
    monkeypatch.setenv("FORUM_COOKIE", "Cookie: sessionid=fromenv")
    session = FakeSession([FakeResponse(200, {"id": 42, "role": "student"})])
    c = ForumClient(session=session, sleep=lambda s: None)
    assert c.me()["id"] == 42
    call = session.calls[0]
    assert call["url"] == "https://forum.minerva.edu/api/v1/users/self"
    assert call["params"] == {"show_inactive": "false"}
    assert call["headers"]["Cookie"] == "sessionid=fromenv"
    assert call["allow_redirects"] is False
    assert normalize_cookie("  'Cookie: a=b'  ") == "a=b"


def test_401_raises_auth_error_telling_user_to_refresh_cookie():
    c, _, _ = client([FakeResponse(401, {"detail": "nope"})])
    with pytest.raises(ForumAuthError, match="Refresh FORUM_COOKIE") as exc:
        c.me()
    assert exc.value.status == 401


def test_403_and_redirect_also_raise_auth_errors():
    c, _, _ = client([FakeResponse(403, {}), FakeResponse(302, None, text="")])
    with pytest.raises(ForumAuthError, match="403"):
        c.me()
    with pytest.raises(ForumAuthError, match="redirected"):
        c.me()


def test_retries_429_and_5xx_with_backoff_and_retry_after():
    c, session, sleeps = client([
        FakeResponse(429, {}, headers={"Retry-After": "2"}),
        FakeResponse(503, {}),
        FakeResponse(200, [{"id": 1}]),
    ], rate_per_sec=0)
    assert c.sections() == [{"id": 1}]
    assert len(session.calls) == 3
    assert sleeps == [2.0, 2.0]  # Retry-After, then backoff 1.0 * 2**1


def test_gives_up_after_max_retries():
    c, _, _ = client([FakeResponse(500, {}) for _ in range(3)], max_retries=2, rate_per_sec=0)
    with pytest.raises(ForumHTTPError) as exc:
        c.me()
    assert exc.value.status == 500


def test_rate_limit_spaces_requests():
    times = iter([0.0, 0.1, 0.4])
    session = FakeSession([FakeResponse(200, {}), FakeResponse(200, {})])
    sleeps = []
    c = ForumClient(COOKIE, session=session, sleep=sleeps.append, clock=lambda: next(times),
                    rate_per_sec=3.0)
    c.me()
    c.me()
    assert sleeps and sleeps[0] == pytest.approx(1 / 3 - 0.1)


def test_sections_past_and_current_params():
    c, session, _ = client([FakeResponse(200, []), FakeResponse(200, {"results": [{"id": 3}]})])
    assert c.sections(past=False) == []
    assert c.sections(past=True) == [{"id": 3}]
    assert session.calls[0]["params"] == {"hide-defaults": "true"}
    assert session.calls[1]["params"] == {"state": "past", "hide-defaults": "true"}


def test_classes_paginates_following_next_and_keeps_section_filter():
    base = "https://forum.minerva.edu/api/v1/paginated-classes"
    pages = [
        FakeResponse(200, {"count": 3, "next": f"{base}?category=past&page=2&page_size=2",
                           "previous": None, "results": [{"id": 1}, {"id": 2}]}),
        FakeResponse(200, {"count": 3, "next": None, "previous": "x", "results": [{"id": 3}]}),
    ]
    c, session, _ = client(pages)
    assert [x["id"] for x in c.classes(77, page_size=2)] == [1, 2, 3]
    first, second = session.calls
    assert first["params"] == {"category": "past", "page_size": 2, "filter_section_id": "77"}
    assert "filter_section_id=77" in second["url"] and second["params"] is None
    assert second["url"].startswith(base)


def test_classes_requires_section_and_aborts_on_org_wide_listing():
    c, _, _ = client([FakeResponse(200, {"count": 11984, "next": "x", "results": []})])
    with pytest.raises(ValueError):
        c.classes(None)
    with pytest.raises(ForumError, match="filter was probably ignored"):
        c.classes(77)


def test_next_link_to_another_host_is_refused():
    pages = [FakeResponse(200, {"count": 2, "next": "https://evil.example.com/steal?page=2",
                                "results": [{"id": 1}]})]
    c, session, _ = client(pages)
    with pytest.raises(ForumError, match="Refusing"):
        c.classes(77)
    assert all("evil" not in call["url"] for call in session.calls)


def test_class_bundle_records_status_of_optional_parts():
    def responder(method, url, kw):
        if url.endswith("/outcome-assessments"):
            return FakeResponse(403, {})
        if url.endswith("/focused-outcomes"):
            return FakeResponse(404, {})
        if url.endswith("/class-events"):
            return FakeResponse(200, [voice_event(1, 0, 1000)])
        if "/analytics/" in url:
            return FakeResponse(403, {})  # staff only
        return FakeResponse(200, {"id": 900001} if url.endswith("900001") else [])

    c, session, _ = client(responder, rate_per_sec=0)
    b = c.class_bundle(900001)
    assert b["class"] == {"id": 900001} and len(b["events"]) == 1
    assert b["status"] == {"class": 200, "classroom": 200, "events": 200, "polls": 200,
                           "poll_responses": 200,
                           "outcome_assessments": 403, "focused_outcomes": 404,
                           "breakouts": 200, "analytics": 403}
    assert b["outcome_assessments"] is None and b["analytics"] is None
    urls = [call["url"] for call in session.calls]
    assert "https://forum.minerva.edu/api/v1/class_grader/classes/900001/poll-responses" in urls
    assert "https://forum.minerva.edu/api/v1/class_grader/classes/900001/breakouts" in urls
    assert "https://forum.minerva.edu/api/v1/analytics/class/900001" in urls
    assert "https://forum.minerva.edu/api/v1/classes/900001" in urls  # has teacher-user-id
    assert not any("class-users" in u for u in urls)


def test_class_users_endpoint_is_never_requested():
    c, session, _ = client([FakeResponse(200, {})])
    for path in ("/api/v1/class-users/123", "api/v1/class-users/9?x=1",
                 "https://forum.minerva.edu/api/v1/class-users/5"):
        with pytest.raises(ForumError, match="write-only"):
            c.get(path)
    assert session.calls == []


def test_probe_capabilities_reports_statuses():
    def responder(method, url, kw):
        if url.endswith("/users/self"):
            return FakeResponse(200, {"id": 2, "role": "student"})
        if "/analytics/" in url:
            return FakeResponse(403, {})
        return FakeResponse(200, {})

    c, session, _ = client(responder, rate_per_sec=0)
    caps = c.probe_capabilities(900001, 77)
    assert caps["role"] == "student"
    assert caps["endpoints"] == {"outcome_assessments": 200, "breakouts": 200, "analytics": 403,
                                 "live_class": 200, "analytics_section": 403}
    assert any(u.endswith("/api/v1/analytics/section/77") for u in
               (call["url"] for call in session.calls))


def test_class_bundle_raises_when_everything_is_forbidden():
    c, _, _ = client(lambda m, u, k: FakeResponse(403, {}), rate_per_sec=0)
    with pytest.raises(ForumAuthError):
        c.class_bundle(1)


def test_recording_urls_use_class_id_and_never_show_signature():
    signed = "https://dexample.cloudfront.net/u/recording.mp4?Expires=1&Signature=SECRETSIG"
    c, session, _ = client([FakeResponse(200, {"highres": signed, "lowres": signed})])
    urls = c.recording_urls(900001)
    assert session.calls[0]["url"].endswith(
        "/api/v1/recording-sessions/900001/generate_recording_urls")
    assert isinstance(urls, SignedUrls) and urls["lowres"] == signed
    assert "SECRETSIG" not in repr(urls) and "SECRETSIG" not in str(urls)
    assert "recording.mp4?<signed>" in repr(urls)


def test_census_counts_types_and_redacts_samples():
    events = [voice_event(1, 0, 1000, name="Secret Person"), voice_event(2, 5, 1000),
              {"event-type": "message", "actor-id": 3,
               "event-data": {"message": "hi", "sender-name": "Secret Person",
                              "email": "x@example.com"}}]
    census = event_census(events, include_sample=True)
    assert list(census) == ["voice", "message"]
    assert census["voice"]["count"] == 2
    assert census["voice"]["data_keys"] == {"duration": ["int"]}
    assert "room-signature" in census["voice"]["keys"]
    sample = census["voice"]["sample"]
    assert sample["actor"]["first-name"] == "[redacted]"
    assert sample["room-signature"] == "[redacted]"
    msg = census["message"]["sample"]["event-data"]
    assert msg["sender-name"] == "[redacted]" and msg["email"] == "[redacted]"
    assert "Secret" not in str(census) and "x@example.com" not in str(census)
    assert "sample" not in event_census(events)["voice"]
    assert census["voice"]["key_styles"] == ["kebab-case"]
    snake = {"event_type": "voice", "actor_id": 1, "event_data": {"duration": 5}}
    assert event_census([snake])["voice"]["key_styles"] == ["snake_case"]
    merged = merge_census([census, event_census(events[:1])])
    assert merged["voice"]["count"] == 3 and merged["voice"]["classes"] == 2
