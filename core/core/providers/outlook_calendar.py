"""Microsoft Graph Calendar — implémentation réelle de CalendarProvider.

Deux points ne sont pas devinables depuis la documentation Microsoft.

**Pas d'id imposable côté client, contrairement à Google Calendar.** L'API
Graph attribue toujours elle-même l'id d'un événement créé — il n'existe pas
d'équivalent au 409-traité-comme-succès de `google_calendar.py`. Un retry
après timeout créerait donc un doublon si rien ne l'en empêchait :
`IdempotencyStore` (`core/idempotency.py`) comble ce manque, comme pour Gmail.

**`calendarView` remplace `singleEvents=true`.** Interroger `/events`
directement renvoie les séries récurrentes comme un seul objet ; `/calendarView`
avec une fenêtre `startDateTime`/`endDateTime` les développe en occurrences,
l'équivalent Microsoft du paramètre `singleEvents` de Google.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..idempotency import IdempotencyStore, InMemoryIdempotencyStore
from ..integrations.http import HttpError, Transport, request_with_retry
from .calendar import Event

API_ROOT = "https://graph.microsoft.com/v1.0/me"


class OutlookCalendar:
    """Implémente le protocole CalendarProvider défini dans calendar.py."""

    def __init__(
        self,
        transport: Transport,
        access_token_provider,
        dedup: IdempotencyStore | None = None,
    ) -> None:
        self._transport = transport
        # Callable async : le jeton est résolu à chaque appel, comme pour Google.
        self._access_token = access_token_provider
        self._dedup = dedup or InMemoryIdempotencyStore()

    async def _call(self, method: str, path: str, *, params=None, json=None, url=None):
        token = await self._access_token()
        response = await request_with_retry(
            self._transport,
            method,
            url or f"{API_ROOT}{path}",
            headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
            params=params,
            json=json,
        )
        return response.json()

    # --- lecture ------------------------------------------------------

    async def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        events: list[Event] = []
        for calendar_id in calendar_ids or ["primary"]:
            path = "/calendarview" if calendar_id == "primary" else f"/calendars/{calendar_id}/calendarview"
            params = {
                "startDateTime": _iso(start),
                "endDateTime": _iso(end),
                "$orderby": "start/dateTime",
                "$top": 250,
            }
            url = None
            while True:
                body = await self._call("GET", path, params=params, url=url)
                for item in body.get("value", []):
                    events.append(_to_event(item, calendar_id))
                url = body.get("@odata.nextLink")
                if not url:
                    break
                params = None  # la pagination est déjà encodée dans nextLink
        return events

    # --- écriture -------------------------------------------------------

    async def create_event(self, event: Event, idempotency_key: str) -> Event:
        cached_id = await self._dedup.get(idempotency_key)
        if cached_id is not None:
            body = await self._call("GET", f"/events/{cached_id}")
            return _to_event(body, event.calendar_id)

        payload = _to_payload(event)
        body = await self._call("POST", "/events", json=payload)
        await self._dedup.put(idempotency_key, body["id"])
        return _to_event(body, event.calendar_id)

    async def update_event(self, event_id: str, patch: dict, idempotency_key: str) -> Event:
        # PATCH est naturellement idempotent — poser deux fois les mêmes champs
        # ne produit rien de plus qu'une fois. Pas de dédoublonnage nécessaire
        # ici, contrairement à `create_event` : c'est déjà le choix fait par
        # `google_calendar.py`.
        calendar_id = patch.pop("calendar_id", "primary")
        body = await self._call("PATCH", f"/events/{event_id}", json=_patch_payload(patch))
        return _to_event(body, calendar_id)

    async def delete_event(self, event_id: str, notify_attendees: bool) -> None:
        try:
            await self._call("DELETE", f"/events/{event_id}")
        except HttpError as exc:
            # Déjà supprimé : l'état voulu est atteint.
            if exc.status_code not in (404, 410):
                raise


# --- conversions ------------------------------------------------------------


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_graph_datetime(node: dict) -> datetime:
    # Le header `Prefer: outlook.timezone="UTC"` (_call) force Graph à renvoyer
    # ses horodatages en UTC, sans le préciser dans le `dateTime` lui-même : la
    # valeur est naïve et doit être traitée comme telle, jamais comme locale.
    parsed = datetime.fromisoformat(node.get("dateTime", ""))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _to_event(item: dict, calendar_id: str) -> Event:
    return Event(
        id=item["id"],
        title=item.get("subject") or "(sans titre)",
        start=_parse_graph_datetime(item.get("start") or {}),
        end=_parse_graph_datetime(item.get("end") or {}),
        attendees=[
            a["emailAddress"]["address"]
            for a in item.get("attendees", [])
            if a.get("emailAddress", {}).get("address")
        ],
        location=(item.get("location") or {}).get("displayName"),
        description=(item.get("body") or {}).get("content"),
        calendar_id=calendar_id,
    )


def _to_payload(event: Event) -> dict:
    payload: dict[str, Any] = {
        "subject": event.title,
        "start": {"dateTime": _iso(event.start), "timeZone": "UTC"},
        "end": {"dateTime": _iso(event.end), "timeZone": "UTC"},
    }
    if event.attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": e}, "type": "required"} for e in event.attendees
        ]
    if event.location:
        payload["location"] = {"displayName": event.location}
    if event.description:
        payload["body"] = {"contentType": "text", "content": event.description}
    return payload


def _patch_payload(patch: dict) -> dict:
    payload: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "title":
            payload["subject"] = value
        elif key in ("start", "end") and isinstance(value, datetime):
            payload[key] = {"dateTime": _iso(value), "timeZone": "UTC"}
        elif key == "attendees":
            payload["attendees"] = [
                {"emailAddress": {"address": e}, "type": "required"} for e in value
            ]
        elif key == "location":
            payload["location"] = {"displayName": value}
        elif key == "description":
            payload["body"] = {"contentType": "text", "content": value}
        else:
            payload[key] = value
    return payload
