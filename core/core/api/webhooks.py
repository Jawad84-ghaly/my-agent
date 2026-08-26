"""Webhook WhatsApp — la partie la plus exposée du système.

Quatre protections, dans cet ordre, avant toute mise en file :

1. Signature HMAC — n'importe qui connaissant l'URL peut sinon piloter l'agent.
2. Fenêtre anti-rejeu — une requête signée capturée ne doit pas être rejouable.
3. Canal vérifié — un numéro inconnu est ignoré en silence, sans réponse (une
   erreur explicite confirmerait à un inconnu que le service existe).
4. Déduplication — Meta et Evolution retentent ; sans cela, les messages sont
   traités en double.

Et une règle de latence : ACK sous 5 secondes, traitement en tâche de fond.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


class WebhookRejected(Exception):
    """Requête refusée avant tout traitement."""


REPLAY_WINDOW_SECONDS = 300


def verify_signature(body: bytes, signature: str, secret: str, timestamp: str | None = None) -> None:
    """Vérifie la signature HMAC-SHA256 du corps brut.

    Le corps *brut* est indispensable : re-sérialiser le JSON change les espaces
    et invalide la signature.
    """
    if not signature:
        raise WebhookRejected("signature absente")

    if timestamp is not None:
        try:
            age = abs(time.time() - float(timestamp))
        except ValueError as exc:
            raise WebhookRejected("horodatage illisible") from exc
        if age > REPLAY_WINDOW_SECONDS:
            raise WebhookRejected(f"horodatage hors fenêtre ({age:.0f}s)")
        payload = f"{timestamp}.".encode() + body
    else:
        payload = body

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=")
    # Comparaison à temps constant : une comparaison naïve fuit la signature.
    if not hmac.compare_digest(expected, provided):
        raise WebhookRejected("signature invalide")


@dataclass
class IncomingMessage:
    id: str
    from_number: str
    kind: str  # text | audio | image
    text: str | None = None
    media_url: str | None = None


def normalize_evolution(payload: dict) -> IncomingMessage | None:
    """Traduit le format Evolution API vers le format interne.

    Retourne None pour tout ce qui n'est pas un message entrant exploitable :
    accusés de lecture, messages sortants, événements de statut.
    """
    if payload.get("event") not in ("messages.upsert", None):
        return None

    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None

    message_id = key.get("id")
    remote_jid = key.get("remoteJid") or ""
    if not message_id or not remote_jid:
        return None

    number = remote_jid.split("@", 1)[0]
    message = data.get("message") or {}

    if "conversation" in message:
        return IncomingMessage(message_id, number, "text", text=message["conversation"])

    extended = message.get("extendedTextMessage")
    if extended and extended.get("text"):
        return IncomingMessage(message_id, number, "text", text=extended["text"])

    audio = message.get("audioMessage")
    if audio:
        return IncomingMessage(message_id, number, "audio", media_url=audio.get("url"))

    image = message.get("imageMessage")
    if image:
        return IncomingMessage(
            message_id, number, "image", text=image.get("caption"), media_url=image.get("url")
        )

    return None


class SeenCache:
    """Déduplication des message_id. En production : Redis avec TTL 24 h."""

    def __init__(self, ttl_seconds: int = 86_400) -> None:
        self.ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    def check_and_add(self, message_id: str, now: float | None = None) -> bool:
        """True si le message est nouveau, False s'il a déjà été traité."""
        now = now if now is not None else time.time()
        self._purge(now)
        if message_id in self._seen:
            return False
        self._seen[message_id] = now
        return True

    def _purge(self, now: float) -> None:
        expired = [k for k, stamp in self._seen.items() if now - stamp > self.ttl]
        for key in expired:
            del self._seen[key]
