"""Google Calendar — implémentation réelle de CalendarProvider.
Trois points méritent une explication, parce qu'ils ne sont pas devinables depuis
la documentation Google.
**Idempotence.** L'API Calendar n'a pas d'en-tête d'idempotence. Le mécanisme
équivalent est d'imposer l'`id` de l'événement : recréer un événement avec un id
existant renvoie 409, qu'on traite comme un succès. On dérive donc l'id de la clé
d'idempotence. Contrainte Google : base32hex minuscule, 5 à 1024 caractères —
d'où l'encodage, un hex classique contiendrait des caractères refusés.
**Sync incrémental.** Le `syncToken` évite de retélécharger tout l'agenda à chaque
tour. Il expire : Google répond alors 410 GONE, et il faut repartir d'une
synchronisation complète. Ne pas gérer ce cas fige la synchro définitivement.
**Journées entières.** Un événement « toute la journée » porte `date` et non
`dateTime`, sans fuseau. Le confondre avec un horodatage décale les rappels d'un
jour entier.
"""
from __future__ import annotations
import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from ..integrations.http import HttpError, Transport, request_with_retry
from .calendar import Event
API_ROOT = "https://www.googleapis.com/calendar/v3"
class SyncTokenExpired(RuntimeError):
    """Le syncToken n'est plus valide : refaire une synchronisation complète."""
@dataclass
class SyncResult:
    events: list[Event]
    deleted_ids: list[str]
    next_sync_token: str | None
def event_id_from_key(idempotency_key: str) -> str:
    """Identifiant déterministe accepté par Google (base32hex minuscule)."""
    digest = hashlib.sha256(idempotency_key.encode()).digest()
    encoded = base64.b32hexencode(digest).decode().lower().rstrip("=")
    return f"nova{encoded[:26]}"
class GoogleCalendar:
    """Implémente le protocole CalendarProvider défini dans calendar.py."""
    def __init__(
        self,
        transport: Transport,
        access_token_provider,
        default_timezone: str = "Europe/Paris",
    ) -> None:
        self._transport = transport
        # Callable async : le jeton est résolu à chaque appel, ce qui laisse le
        # rafraîchissement OAuth se faire de façon transparente.
        self._access_token = access_token_provider
        self._timezone = default_timezone
    async def _call(self, method: str, path: str, *, params=None, json=None):
        token = await self._access_token()
        response = await request_with_retry(
            self._transport,
            method,
            f"{API_ROOT}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json,
        )
        return response.json()
    # --- lecture ----------------------------------------------------------
    async def list_events(
        self, start: datetime, end: datetime, calendar_ids: list[str] | None = None
    ) -> list[Event]:
        events: list[Event] = []
        for calendar_id in calendar_ids or ["primary"]:
            page_token = None
            while True:
                params = {
                    "timeMin": _rfc3339(start),
                    "timeMax": _rfc3339(end),
                    "singleEvents": "true",  # développe les récurrences
                    "orderBy": "startTime",
                    "maxResults": 250,
                }
                if page_token:
                    params["pageToken"] = page_token
                body = await self._call("GET", f"/calendars/{calendar_id}/events", params=params)
                for item in body.get("items", []):
                    parsed = _to_event(item, calendar_id)
                    if parsed is not None:
                        events.append(parsed)
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
        return events
    async def sync(self, calendar_id: str = "primary", sync_token: str | None = None) -> SyncResult:
        """Synchronisation incrémentale. Lève SyncTokenExpired sur 410."""
        params: dict[str, Any] = {"singleEvents": "true", "maxResults": 250}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            # Première synchro : fenêtre bornée, sinon on tire tout l'historique.
            params["timeMin"] = _rfc3339(datetime.now(timezone.utc) - timedelta(days=30))
        events: list[Event] = []
        deleted: list[str] = []
        page_token = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            try:
                body = await self._call("GET", f"/calendars/{calendar_id}/events", params=params)
            except HttpError as exc:
                if exc.status_code == 410:
                    raise SyncTokenExpired(
                        "syncToken expiré, une synchronisation complète est requise"
                    ) from exc
                raise
            for item in body.get("items", []):
                if item.get("status") == "cancelled":
                    deleted.append(item["id"])
                    continue
                parsed = _to_event(item, calendar_id)
                if parsed is not None:
                    events.append(parsed)
            page_token = body.get("nextPageToken")
            if not page_token:
                return SyncResult(events, deleted, body.get("nextSyncToken"))
    # --- écriture ---------------------------------------------------------
    async def create_event(self, event: Event, idempotency_key: str) -> Event:
        event_id = event_id_from_key(idempotency_key)
        payload = _to_payload(event, self._timezone) | {"id": event_id}
        params = {"sendUpdates": "all" if event.attendees else "none"}
        try:
            body = await self._call(
                "POST",
                f"/calendars/{event.calendar_id}/events",
                params=params,
                json=payload,
            )
        except HttpError as exc:
            # 409 : l'identifiant existe déjà, donc un appel précédent a abouti.
            # C'est un succès, pas une erreur — sinon un retry après timeout
            # remonterait un échec pour un événement bel et bien créé.
            if exc.status_code == 409:
                body = await self._call("GET", f"/calendars/{event.calendar_id}/events/{event_id}")
            else:
                raise
        return _to_event(body, event.calendar_id) or event
    async def update_event(self, event_id: str, patch: dict, idempotency_key: str) -> Event:
        calendar_id = patch.pop("calendar_id", "primary")
        body = await self._call(
            "PATCH",
            f"/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": "all" if patch.get("attendees") else "none"},
            json=_patch_payload(patch, self._timezone),
        )
        return _to_event(body, calendar_id)
    async def delete_event(self, event_id: str, notify_attendees: bool) -> None:
        try:
            await self._call(
                "DELETE",
                f"/calendars/primary/events/{event_id}",
                params={"sendUpdates": "all" if notify_attendees else "none"},
            )
        except HttpError as exc:
            # Déjà supprimé : l'état voulu est atteint.
            if exc.status_code not in (404, 410):
                raise
# --- conversions ----------------------------------------------------------
def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()
def _parse_google_datetime(node: dict) -> tuple[datetime, bool] | None:
    """Renvoie (datetime, journée_entière) ou None si le nœud est inexploitable."""
    if "dateTime" in node:
        raw = node["dateTime"].replace("Z", "+00:00")
        return datetime.fromisoformat(raw), False
    if "date" in node:
        day = date.fromisoformat(node["date"])
        return datetime.combine(day, time.min, tzinfo=timezone.utc), True
    return None
def _to_event(item: dict, calendar_id: str) -> Event | None:
    start = _parse_google_datetime(item.get("start") or {})
    end = _parse_google_datetime(item.get("end") or {})
    if start is None or end is None:
        return None  # événement sans horaire exploitable
    return Event(
        id=item["id"],
        title=item.get("summary") or "(sans titre)",
        start=start[0],
        end=end[0],
        attendees=[a["email"] for a in item.get("attendees", []) if a.get("email")],
        location=item.get("location"),
        description=item.get("description"),
        calendar_id=calendar_id,
    )
def _to_payload(event: Event, tz: str) -> dict:
    payload: dict[str, Any] = {
        "summary": event.title,
        "start": {"dateTime": _rfc3339(event.start), "timeZone": tz},
        "end": {"dateTime": _rfc3339(event.end), "timeZone": tz},
    }
    if event.attendees:
        payload["attendees"] = [{"email": e} for e in event.attendees]
    if event.location:
        payload["location"] = event.location
    if event.description:
        payload["description"] = event.description
    return payload
def _patch_payload(patch: dict, tz: str) -> dict:
    payload: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "title":
            payload["summary"] = value
        elif key in ("start", "end") and isinstance(value, datetime):
            payload[key] = {"dateTime": _rfc3339(value), "timeZone": tz}
        elif key == "attendees":
            payload["attendees"] = [{"email": e} for e in value]
        else:
            payload[key] = value
    return payload
