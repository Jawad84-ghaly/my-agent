"""Abstraction calendrier — Google et Outlook derrière une seule interface.

Le reste du code ne connaît que `CalendarProvider`. C'est ce qui permet de
supporter les deux fournisseurs sans dupliquer la logique agentique, et de tester
tout le graphe sans réseau grâce à `InMemoryCalendar`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str | None = None
    description: str | None = None
    calendar_id: str = "primary"

    def overlaps(self, start: datetime, end: datetime) -> bool:
        # Bornes exclusives : un RDV 9h-10h ne chevauche pas un RDV 10h-11h.
        return self.start < end and start < self.end


@dataclass
class Slot:
    start: datetime
    end: datetime


@dataclass
class Conflict:
    event: Event
    overlap_minutes: int


class CalendarProvider(Protocol):
    async def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]: ...

    async def create_event(self, event: Event, idempotency_key: str) -> Event: ...

    async def update_event(self, event_id: str, patch: dict, idempotency_key: str) -> Event: ...

    async def delete_event(self, event_id: str, notify_attendees: bool) -> None: ...


async def detect_conflicts(
    provider: CalendarProvider, start: datetime, end: datetime
) -> list[Conflict]:
    """Appelé avant toute création. On ne crée jamais silencieusement par-dessus."""
    events = await provider.list_events(start - timedelta(hours=12), end + timedelta(hours=12))
    conflicts = []
    for event in events:
        if not event.overlaps(start, end):
            continue
        overlap = min(event.end, end) - max(event.start, start)
        conflicts.append(Conflict(event, int(overlap.total_seconds() // 60)))
    return sorted(conflicts, key=lambda c: -c.overlap_minutes)


async def find_free_slots(
    provider: CalendarProvider,
    duration_min: int,
    window_start: datetime,
    window_end: datetime,
    working_hours: tuple[int, int] = (9, 18),
    granularity_min: int = 15,
) -> list[Slot]:
    """Créneaux libres dans une fenêtre, alignés sur la granularité demandée."""
    events = await provider.list_events(window_start, window_end)
    busy = sorted((e for e in events), key=lambda e: e.start)
    duration = timedelta(minutes=duration_min)
    step = timedelta(minutes=granularity_min)

    slots: list[Slot] = []
    cursor = _ceil_to(window_start, step)
    while cursor + duration <= window_end:
        candidate_end = cursor + duration
        in_hours = working_hours[0] <= cursor.hour and candidate_end.hour <= working_hours[1]
        if in_hours and not any(e.overlaps(cursor, candidate_end) for e in busy):
            slots.append(Slot(cursor, candidate_end))
            cursor = candidate_end
        else:
            cursor += step
    return slots


def _ceil_to(moment: datetime, step: timedelta) -> datetime:
    seconds = int(step.total_seconds())
    stamp = int(moment.timestamp())
    remainder = stamp % seconds
    return moment if remainder == 0 else moment + timedelta(seconds=seconds - remainder)


class InMemoryCalendar:
    """Implémentation de test. Respecte l'idempotence, comme les vraies."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self.events: dict[str, Event] = {e.id: e for e in (events or [])}
        self._seen_keys: dict[str, Event] = {}

    async def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        wanted = set(calendar_ids or ["primary"])
        return [
            e
            for e in self.events.values()
            if e.calendar_id in wanted and e.overlaps(start, end)
        ]

    async def create_event(self, event: Event, idempotency_key: str) -> Event:
        # Un retry après timeout réseau ne doit pas créer un second RDV.
        if idempotency_key in self._seen_keys:
            return self._seen_keys[idempotency_key]
        self.events[event.id] = event
        self._seen_keys[idempotency_key] = event
        return event

    async def update_event(self, event_id: str, patch: dict, idempotency_key: str) -> Event:
        if idempotency_key in self._seen_keys:
            return self._seen_keys[idempotency_key]
        event = self.events[event_id]
        for key, value in patch.items():
            setattr(event, key, value)
        self._seen_keys[idempotency_key] = event
        return event

    async def delete_event(self, event_id: str, notify_attendees: bool) -> None:
        self.events.pop(event_id, None)
