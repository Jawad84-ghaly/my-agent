"""Gmail — implémentation réelle de MailProvider.

Un point mérite une explication, parce qu'il n'est pas devinable depuis la
documentation Google : **pas d'id imposable côté client.** Contrairement à
Calendar, ni `drafts.create` ni `drafts.send` n'acceptent d'identifiant fourni
par l'appelant — Google en génère toujours un nouveau. Un retry après timeout
enverrait donc un second email identique si rien ne l'en empêchait. C'est le
rôle de l'`IdempotencyStore` injecté : la clé est vérifiée avant l'appel HTTP
et enregistrée après, ce que ferait un en-tête `Idempotency-Key` si Gmail en
proposait un.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText

from ..integrations.http import Transport, request_with_retry
from .mail import Draft, IdempotencyStore, InMemoryIdempotencyStore, SentMessage

API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"


def _encode_mime(to: list[str], subject: str, body: str, cc: list[str] | None = None) -> str:
    message = MIMEText(body)
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if cc:
        message["Cc"] = ", ".join(cc)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


class GmailProvider:
    """Implémente le protocole MailProvider défini dans mail.py."""

    def __init__(
        self,
        transport: Transport,
        access_token_provider,
        dedup: IdempotencyStore | None = None,
    ) -> None:
        self._transport = transport
        # Callable async : le jeton est résolu à chaque appel, comme pour Calendar.
        self._access_token = access_token_provider
        self._dedup = dedup or InMemoryIdempotencyStore()

    async def _call(self, method: str, path: str, *, json=None):
        token = await self._access_token()
        response = await request_with_retry(
            self._transport,
            method,
            f"{API_ROOT}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json,
        )
        return response.json()

    async def create_draft(
        self, to: list[str], subject: str, body: str, idempotency_key: str, cc: list[str] | None = None
    ) -> Draft:
        cached = await self._dedup.get(idempotency_key)
        if cached is not None:
            return Draft(id=cached, to=to, subject=subject, body=body, cc=cc or [])

        raw = _encode_mime(to, subject, body, cc)
        response = await self._call("POST", "/drafts", json={"message": {"raw": raw}})
        draft_id = response["id"]
        await self._dedup.put(idempotency_key, draft_id)
        return Draft(id=draft_id, to=to, subject=subject, body=body, cc=cc or [])

    async def send_draft(self, draft_id: str, idempotency_key: str) -> SentMessage:
        cached = await self._dedup.get(idempotency_key)
        if cached is not None:
            return SentMessage(id=cached, thread_id="", to=[], subject="")

        response = await self._call("POST", "/drafts/send", json={"id": draft_id})
        message_id = response["id"]
        await self._dedup.put(idempotency_key, message_id)
        return SentMessage(id=message_id, thread_id=response.get("threadId", ""), to=[], subject="")
