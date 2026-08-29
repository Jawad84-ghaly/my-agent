"""Outils `calendar.*`, liés à un provider Google.

`contacts.resolve` est référencé par le prompt du planificateur mais n'a pas
d'implémentation (pas de People API) ; un plan qui l'invoque le voit écarté
comme outil inconnu, sans planter. `mail.draft`/`mail.send` sont eux
implémentés — voir `tools/mail_tools.py`.
`mail.draft`/`mail.send` sont référencés par le prompt du planificateur mais
n'ont pas d'implémentation (pas de Gmail) ; un plan qui les invoque les voit
écartés comme outils inconnus, sans planter. `contacts.resolve`/`contacts.get`
sont eux implémentés — voir `tools/contacts_tools.py`.

Les arguments arrivent en JSON depuis le plan du modèle — des dates en ISO 8601,
jamais des `datetime` — et les résultats repartent en dict JSON-sérialisable pour
la même raison, dans l'autre sens.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..providers.calendar import CalendarProvider, Event, detect_conflicts, find_free_slots
from .registry import ToolRegistry


def _parse(moment: str) -> datetime:
    parsed = datetime.fromisoformat(moment)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_dict(event: Event) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "attendees": event.attendees,
        "location": event.location,
        "description": event.description,
        "calendar_id": event.calendar_id,
    }


def register_calendar_tools(registry: ToolRegistry, provider: CalendarProvider) -> None:
    """Enregistre les outils `calendar.*` liés à ce provider — donc à cet utilisateur.

    Un `ToolRegistry` neuf par job, pas un registre partagé : le provider est
    fermé sur les identifiants d'un seul utilisateur (`access_token_provider`),
    donc le registre qui le porte ne peut pas être partagé entre deux jobs sans
    faire fuiter le calendrier de l'un vers l'autre.
    """

    @registry.register(
        "calendar.list_events", description="Liste les événements dans une fenêtre de temps."
    )
    async def _list_events(
        start: str, end: str, calendar_ids: list[str] | None = None
    ) -> list[dict]:
        events = await provider.list_events(_parse(start), _parse(end), calendar_ids)
        return [_event_dict(e) for e in events]

    @registry.register(
        "calendar.detect_conflicts",
        description="Détecte les chevauchements avant de créer un événement.",
    )
    async def _detect_conflicts(start: str, end: str) -> list[dict]:
        conflicts = await detect_conflicts(provider, _parse(start), _parse(end))
        return [
            {"event": _event_dict(c.event), "overlap_minutes": c.overlap_minutes}
            for c in conflicts
        ]

    @registry.register(
        "calendar.find_free_slots",
        description="Créneaux libres dans une fenêtre, pour proposer un horaire.",
    )
    async def _find_free_slots(
        duration_min: int, window_start: str, window_end: str
    ) -> list[dict]:
        slots = await find_free_slots(provider, duration_min, _parse(window_start), _parse(window_end))
        return [{"start": s.start.isoformat(), "end": s.end.isoformat()} for s in slots]

    @registry.register(
        "calendar.create_event", mutating=True, description="Crée un événement. Idempotent."
    )
    async def _create_event(
        title: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
        calendar_id: str = "primary",
        idempotency_key: str = "",
    ) -> dict:
        event = Event(
            id="",
            title=title,
            start=_parse(start),
            end=_parse(end),
            attendees=attendees or [],
            location=location,
            description=description,
            calendar_id=calendar_id,
        )
        created = await provider.create_event(event, idempotency_key)
        return _event_dict(created)

    @registry.register(
        "calendar.update_event", mutating=True, description="Modifie un événement existant."
    )
    async def _update_event(event_id: str, idempotency_key: str = "", **patch) -> dict:
        for key in ("start", "end"):
            if isinstance(patch.get(key), str):
                patch[key] = _parse(patch[key])
        updated = await provider.update_event(event_id, patch, idempotency_key)
        return _event_dict(updated)

    @registry.register(
        "calendar.delete_event", mutating=True, description="Supprime un événement."
    )
    async def _delete_event(
        event_id: str, notify_attendees: bool = True, idempotency_key: str = ""
    ) -> dict:
        await provider.delete_event(event_id, notify_attendees)
        return {"deleted": event_id}
