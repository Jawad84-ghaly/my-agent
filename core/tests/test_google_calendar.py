import asyncio
from datetime import datetime, timezone
import pytest
from core.integrations.http import HttpError, Response, request_with_retry
from core.providers.calendar import Event
from core.providers.google_calendar import (
    GoogleCalendar,
    SyncTokenExpired,
    event_id_from_key,
)
from conftest import FakeTransport, always, sequence
def run(coro):
    return asyncio.run(coro)
async def token():
    return "at-1"
def google_event(**overrides) -> dict:
    base = {
        "id": "evt-1",
        "summary": "Point produit",
        "start": {"dateTime": "2026-08-27T10:00:00+02:00"},
        "end": {"dateTime": "2026-08-27T11:00:00+02:00"},
        "attendees": [{"email": "marc@exemple.fr"}],
    }
    return base | overrides
def calendar_with(handler) -> tuple[GoogleCalendar, FakeTransport]:
    transport = FakeTransport(handler)
    return GoogleCalendar(transport, token), transport
# --- lecture et conversion -------------------------------------------------
def test_events_are_converted_with_timezone():
    cal, _ = calendar_with(always(Response(200, {"items": [google_event()]})))
    events = run(cal.list_events(datetime(2026, 8, 27, tzinfo=timezone.utc),
                                 datetime(2026, 8, 28, tzinfo=timezone.utc)))
    assert len(events) == 1
    assert events[0].title == "Point produit"
    assert events[0].start.hour == 10
    assert events[0].start.utcoffset().total_seconds() == 7200
    assert events[0].attendees == ["marc@exemple.fr"]
def test_all_day_events_are_not_read_as_timestamps():
    """Confondre `date` et `dateTime` décale les rappels d'une journée entière."""
    item = google_event(start={"date": "2026-08-27"}, end={"date": "2026-08-28"})
    cal, _ = calendar_with(always(Response(200, {"items": [item]})))
    events = run(cal.list_events(datetime(2026, 8, 1, tzinfo=timezone.utc),
                                 datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert events[0].start.date().isoformat() == "2026-08-27"
    assert events[0].start.hour == 0
def test_events_without_usable_time_are_skipped():
    cal, _ = calendar_with(always(Response(200, {"items": [google_event(start={}, end={})]})))
    events = run(cal.list_events(datetime(2026, 8, 27, tzinfo=timezone.utc),
                                 datetime(2026, 8, 28, tzinfo=timezone.utc)))
    assert events == []
def test_recurring_events_are_expanded():
    cal, transport = calendar_with(always(Response(200, {"items": []})))
    run(cal.list_events(datetime(2026, 8, 27, tzinfo=timezone.utc),
                        datetime(2026, 8, 28, tzinfo=timezone.utc)))
    assert transport.requests[0].params["singleEvents"] == "true"
def test_pagination_is_followed():
    def handler(_req, index):
        if index == 0:
            return Response(200, {"items": [google_event(id="a")], "nextPageToken": "p2"})
        return Response(200, {"items": [google_event(id="b")]})
    cal, transport = calendar_with(handler)
    events = run(cal.list_events(datetime(2026, 8, 27, tzinfo=timezone.utc),
                                 datetime(2026, 8, 28, tzinfo=timezone.utc)))
    assert [e.id for e in events] == ["a", "b"]
    assert len(transport.requests) == 2
# --- idempotence -----------------------------------------------------------
def test_event_id_is_deterministic_and_valid_for_google():
    a = event_id_from_key("key-abc")
    b = event_id_from_key("key-abc")
    assert a == b
    assert a != event_id_from_key("key-xyz")
    # base32hex minuscule, longueur acceptée par l'API
    assert a.islower() and 5 <= len(a) <= 1024
    assert all(c in "0123456789abcdefghijklmnopqrstuv" for c in a)
def test_create_sets_the_derived_id():
    cal, transport = calendar_with(always(Response(200, google_event())))
    event = Event("ignored", "Point", datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 11, tzinfo=timezone.utc))
    run(cal.create_event(event, "key-abc"))
    assert transport.requests[0].json["id"] == event_id_from_key("key-abc")
def test_conflict_on_retry_is_treated_as_success():
    """Un retry après timeout ne doit pas remonter un échec pour un RDV créé."""
    def handler(request, _index):
        if request.method == "POST":
            return Response(409, {"error": {"message": "The requested identifier already exists"}})
        return Response(200, google_event())
    cal, transport = calendar_with(handler)
    event = Event("x", "Point", datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 11, tzinfo=timezone.utc))
    result = run(cal.create_event(event, "key-abc"))
    assert result.title == "Point produit"
    assert transport.requests[-1].method == "GET"
def test_other_errors_still_propagate():
    cal, _ = calendar_with(always(Response(403, {"error": {"message": "forbidden"}})))
    event = Event("x", "Point", datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 11, tzinfo=timezone.utc))
    with pytest.raises(HttpError):
        run(cal.create_event(event, "key-abc"))
# --- notifications aux participants ---------------------------------------
def test_invitations_are_sent_only_when_there_are_attendees():
    cal, transport = calendar_with(always(Response(200, google_event())))
    solo = Event("x", "Bloquer 2 h", datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
                 datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    run(cal.create_event(solo, "key-1"))
    assert transport.requests[0].params["sendUpdates"] == "none"
    cal2, transport2 = calendar_with(always(Response(200, google_event())))
    shared = Event("x", "Point", datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
                   datetime(2026, 8, 27, 11, tzinfo=timezone.utc),
                   attendees=["marc@exemple.fr"])
    run(cal2.create_event(shared, "key-2"))
    assert transport2.requests[0].params["sendUpdates"] == "all"
def test_deleting_an_absent_event_is_not_an_error():
    cal, _ = calendar_with(always(Response(404, {"error": {"message": "Not Found"}})))
    run(cal.delete_event("evt-gone", notify_attendees=False))  # ne lève pas
# --- synchronisation incrémentale -----------------------------------------
def test_sync_returns_events_deletions_and_next_token():
    body = {
        "items": [google_event(id="a"), {"id": "b", "status": "cancelled"}],
        "nextSyncToken": "tok-2",
    }
    cal, _ = calendar_with(always(Response(200, body)))
    result = run(cal.sync(sync_token="tok-1"))
    assert [e.id for e in result.events] == ["a"]
    assert result.deleted_ids == ["b"]
    assert result.next_sync_token == "tok-2"
def test_expired_sync_token_raises_a_specific_error():
    """Sans ce cas, la synchro reste figée indéfiniment."""
    cal, _ = calendar_with(always(Response(410, {"error": {"message": "Sync token is no longer valid"}})))
    with pytest.raises(SyncTokenExpired):
        run(cal.sync(sync_token="tok-old"))
def test_first_sync_bounds_the_window():
    cal, transport = calendar_with(always(Response(200, {"items": [], "nextSyncToken": "t"})))
    run(cal.sync())
    assert "timeMin" in transport.requests[0].params  # pas tout l'historique
# --- retry HTTP ------------------------------------------------------------
def test_rate_limit_is_retried_then_succeeds():
    transport = FakeTransport(
        sequence(Response(429, None, {"Retry-After": "2"}), Response(200, {"ok": True}))
    )
    response = run(request_with_retry(transport, "GET", "https://x/y", sleep=transport.sleep))
    assert response.json() == {"ok": True}
    assert transport.slept == [2.0]
def test_google_quota_403_is_recognised_as_rate_limiting():
    """Google renvoie parfois 403 plutôt que 429 pour un dépassement de quota."""
    quota = Response(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
    transport = FakeTransport(sequence(quota, Response(200, {"ok": True})))
    response = run(request_with_retry(transport, "GET", "https://x/y", sleep=transport.sleep))
    assert response.ok
    assert len(transport.requests) == 2
def test_permission_403_is_not_retried():
    forbidden = Response(403, {"error": {"errors": [{"reason": "forbidden"}], "message": "no"}})
    transport = FakeTransport(always(forbidden))
    with pytest.raises(HttpError):
        run(request_with_retry(transport, "GET", "https://x/y", sleep=transport.sleep))
    assert len(transport.requests) == 1
def test_retries_are_bounded():
    transport = FakeTransport(always(Response(503, None)))
    with pytest.raises(HttpError) as exc:
        run(request_with_retry(transport, "GET", "https://x/y", sleep=transport.sleep))
    assert exc.value.status_code == 503
    assert len(transport.requests) == 3
def test_client_error_surfaces_the_google_message():
    transport = FakeTransport(always(Response(400, {"error": {"message": "Invalid time range"}})))
    with pytest.raises(HttpError, match="Invalid time range"):
        run(request_with_retry(transport, "GET", "https://x/y", sleep=transport.sleep))
