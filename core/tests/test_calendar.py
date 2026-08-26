import asyncio
from datetime import datetime, timedelta, timezone

from core.providers.calendar import Event, InMemoryCalendar, detect_conflicts, find_free_slots


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 27, hour, minute, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def test_adjacent_events_do_not_conflict():
    """9h-10h et 10h-11h se suivent, ils ne se chevauchent pas."""
    cal = InMemoryCalendar([Event("e1", "Existant", at(9), at(10))])
    assert run(detect_conflicts(cal, at(10), at(11))) == []


def test_overlap_is_detected_and_measured():
    cal = InMemoryCalendar([Event("e1", "Existant", at(9, 30), at(10, 30))])
    conflicts = run(detect_conflicts(cal, at(10), at(11)))
    assert len(conflicts) == 1
    assert conflicts[0].overlap_minutes == 30


def test_conflicts_are_sorted_by_severity():
    cal = InMemoryCalendar(
        [
            Event("e1", "Léger", at(10, 45), at(11, 30)),
            Event("e2", "Franc", at(10), at(11)),
        ]
    )
    conflicts = run(detect_conflicts(cal, at(10), at(11)))
    assert [c.event.id for c in conflicts] == ["e2", "e1"]


def test_free_slots_avoid_busy_periods():
    cal = InMemoryCalendar([Event("e1", "Occupé", at(10), at(11))])
    slots = run(find_free_slots(cal, 60, at(9), at(13)))
    starts = [s.start.hour for s in slots]
    assert 9 in starts
    assert 10 not in starts
    assert 11 in starts


def test_free_slots_respect_working_hours():
    cal = InMemoryCalendar()
    slots = run(find_free_slots(cal, 60, at(6), at(23), working_hours=(9, 18)))
    assert all(s.start.hour >= 9 and s.end.hour <= 18 for s in slots)


def test_create_is_idempotent_under_retry():
    """Un timeout réseau suivi d'un retry ne doit pas créer deux réunions."""
    cal = InMemoryCalendar()
    event = Event("evt-1", "Point", at(10), at(11))
    run(cal.create_event(event, "key-abc"))
    run(cal.create_event(event, "key-abc"))
    assert len(cal.events) == 1


def test_different_keys_create_different_events():
    cal = InMemoryCalendar()
    run(cal.create_event(Event("evt-1", "A", at(10), at(11)), "key-1"))
    run(cal.create_event(Event("evt-2", "B", at(14), at(15)), "key-2"))
    assert len(cal.events) == 2


def test_list_events_filters_by_window():
    cal = InMemoryCalendar(
        [
            Event("e1", "Aujourd'hui", at(10), at(11)),
            Event("e2", "Demain", at(10) + timedelta(days=1), at(11) + timedelta(days=1)),
        ]
    )
    events = run(cal.list_events(at(0), at(23, 59)))
    assert [e.id for e in events] == ["e1"]
