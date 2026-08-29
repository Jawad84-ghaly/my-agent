"""Abstraction messagerie — Gmail et Outlook derrière une seule interface.

Même logique que `providers/calendar.py` : le reste du code ne connaît que
`MailProvider`, ce qui permet de tester tout le graphe hors ligne via
`InMemoryMailbox`. L'idempotence (`IdempotencyStore`, dans `core/idempotency.py`)
est partagée avec le provider Outlook Calendar : ni Gmail ni Microsoft Graph
n'acceptent d'id imposé par le client, contrairement à Google Calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..idempotency import InMemoryIdempotencyStore


@dataclass
class Draft:
    id: str
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)


@dataclass
class SentMessage:
    id: str
    thread_id: str
    to: list[str]
    subject: str


class MailProvider(Protocol):
    async def create_draft(
        self, to: list[str], subject: str, body: str, idempotency_key: str, cc: list[str] | None = None
    ) -> Draft: ...

    async def send_draft(self, draft_id: str, idempotency_key: str) -> SentMessage: ...


class InMemoryMailbox:
    """Implémentation de test. Respecte l'idempotence, comme la vraie."""

    def __init__(self) -> None:
        self.drafts: dict[str, Draft] = {}
        self.sent: list[SentMessage] = []
        self._next_id = 0
        self._dedup = InMemoryIdempotencyStore()

    def _fresh_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    async def create_draft(
        self, to: list[str], subject: str, body: str, idempotency_key: str, cc: list[str] | None = None
    ) -> Draft:
        cached = await self._dedup.get(idempotency_key)
        if cached is not None:
            return self.drafts[cached]
        draft = Draft(id=self._fresh_id("draft"), to=to, subject=subject, body=body, cc=cc or [])
        self.drafts[draft.id] = draft
        await self._dedup.put(idempotency_key, draft.id)
        return draft

    async def send_draft(self, draft_id: str, idempotency_key: str) -> SentMessage:
        cached = await self._dedup.get(idempotency_key)
        if cached is not None:
            return next(m for m in self.sent if m.id == cached)
        draft = self.drafts[draft_id]
        message = SentMessage(id=self._fresh_id("msg"), thread_id=self._fresh_id("thread"),
                               to=draft.to, subject=draft.subject)
        self.sent.append(message)
        await self._dedup.put(idempotency_key, message.id)
        return message
