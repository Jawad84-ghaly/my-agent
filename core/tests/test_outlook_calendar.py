import asyncio
from datetime import datetime, timezone

import pytest

from core.integrations.http import HttpError, Response
from core.providers.calendar import Event
from core.providers.outlook_calendar import OutlookCalendar

from conftest import FakeTransport, always


def run(coro):
    return asyncio.run(coro)


async def token():
    return "at-1"


def graph_event(**overrides) -> dict:
    base = {
        "id": "evt-1",
        "subject": "Point produit",
        "start": {"dateTime": "2026-08-27T08:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-08-27T09:00:00.0000000", "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": "marc@exemple.fr"}}],
    }
    return base | overrides


def calendar_with(handler) -> tuple[OutlookCalendar, FakeTransport]:
    transport = FakeTransport(handler)
    return OutlookCalendar(transport, token), transport


# --- lecture et conversion -------------------------------------------------


def test_events_are_converted_as_utc():
    cal, transport = calendar_with(always(Response(200, {"value": [graph_event()]})))
    events = run(cal.list_events(
        datetime(2026, 8, 27, tzinfo=timezone.utc), datetime(2026, 8, 28, tzinfo=timezone.utc)
    ))
    assert len(events) == 1
    assert events[0].title == "Point produit"
    assert events[0].start.hour == 8
    assert events[0].attendees == ["marc@exemple.fr"]
    assert transport.requests[0].url.endswith("/calendarview")


def test_pagination_follows_odata_next_link():
    def handler(request, _index):
        if "@odata.nextLink" not in request.url:
            return Response(200, {"value": [graph_event(id="a")],
                                   "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarview?@odata.nextLink=p2"})
        return Response(200, {"value": [graph_event(id="b")]})

    cal, transport = calendar_with(handler)
    events = run(cal.list_events(
        datetime(2026, 8, 27, tzinfo=timezone.utc), datetime(2026, 8, 28, tzinfo=timezone.utc)
    ))
    assert [e.id for e in events] == ["a", "b"]
    assert len(transport.requests) == 2


# --- idempotence -------------------------------------------------------


def test_create_is_idempotent_under_retry():
    """Microsoft Graph n'accepte pas d'id imposé par le client, contrairement à
    Google : sans le store de dédoublonnage, un retry créerait un doublon."""
    cal, transport = calendar_with(always(Response(200, graph_event())))
    event = Event("ignored", "Point", datetime(2026, 8, 27, 8, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 9, tzinfo=timezone.utc))
    first = run(cal.create_event(event, "key-abc"))
    second = run(cal.create_event(event, "key-abc"))
    assert first.id == second.id == "evt-1"
    # Le second appel relit l'événement déjà créé, il ne recrée jamais.
    assert transport.requests[0].method == "POST"
    assert transport.requests[1].method == "GET"


def test_different_keys_create_different_events():
    cal, transport = calendar_with(always(Response(200, graph_event())))
    event = Event("ignored", "Point", datetime(2026, 8, 27, 8, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 9, tzinfo=timezone.utc))
    run(cal.create_event(event, "key-1"))
    run(cal.create_event(event, "key-2"))
    assert [r.method for r in transport.requests] == ["POST", "POST"]


def test_other_errors_still_propagate():
    cal, _ = calendar_with(always(Response(403, {"error": {"message": "forbidden"}})))
    event = Event("x", "Point", datetime(2026, 8, 27, 8, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 9, tzinfo=timezone.utc))
    with pytest.raises(HttpError):
        run(cal.create_event(event, "key-err"))


def test_deleting_an_absent_event_is_not_an_error():
    cal, _ = calendar_with(always(Response(404, {"error": {"message": "Not Found"}})))
    run(cal.delete_event("evt-gone", notify_attendees=False))  # ne lève pas


# --- payloads Graph ----------------------------------------------------


def test_create_payload_uses_graph_field_names():
    cal, transport = calendar_with(always(Response(200, graph_event())))
    event = Event("x", "Point", datetime(2026, 8, 27, 8, tzinfo=timezone.utc),
                  datetime(2026, 8, 27, 9, tzinfo=timezone.utc),
                  attendees=["marc@exemple.fr"], location="Salle A", description="Ordre du jour")
    run(cal.create_event(event, "key-1"))
    payload = transport.requests[0].json
    assert payload["subject"] == "Point"
    assert payload["attendees"][0]["emailAddress"]["address"] == "marc@exemple.fr"
    assert payload["location"]["displayName"] == "Salle A"
    assert payload["body"]["content"] == "Ordre du jour"
