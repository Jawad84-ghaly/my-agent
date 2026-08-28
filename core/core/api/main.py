"""Application FastAPI — la porte d'entrée de tous les canaux.
Le gateway ne contient aucune logique agentique : il authentifie, normalise,
met en file, et rend la main. Tout le raisonnement vit dans le graphe.
"""
from __future__ import annotations
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Header, Request, Response
from ..db.repositories import PostgresChannelRegistry, PostgresSeenCache
from ..db.session import database_url, make_engine, make_session_factory, session_scope
from ..workers import Priority
from .webhooks import WebhookRejected, normalize_evolution, verify_signature
log = logging.getLogger("nova.api")
WHATSAPP_SECRET = os.environ.get("NOVA_WHATSAPP_WEBHOOK_SECRET", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Un engine et un pool Redis par processus, pas par requête.

    `session_scope` ouvre ensuite une session par webhook — ce sont deux
    granularités différentes, la connexion partagée et la transaction unitaire.
    """
    engine = make_engine(database_url())
    app.state.session_factory = make_session_factory(engine)
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    try:
        yield
    finally:
        await app.state.arq_pool.close(close_connection_pool=True)
        await engine.dispose()


app = FastAPI(title="Nova Core", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_hub_timestamp: str | None = Header(default=None),
) -> Response:
    """Reçoit un message WhatsApp.
    Répond systématiquement 200, même sur rejet : un code d'erreur déclencherait
    des relances côté Meta/Evolution et révélerait la politique de filtrage.
    """
    body = await request.body()
    try:
        verify_signature(body, x_hub_signature_256, WHATSAPP_SECRET, x_hub_timestamp)
    except WebhookRejected as exc:
        log.warning("webhook refusé: %s", exc)
        return Response(status_code=200)
    payload = await request.json()
    message = normalize_evolution(payload)
    if message is None:
        return Response(status_code=200)

    async with session_scope(request.app.state.session_factory) as session:
        seen = PostgresSeenCache(session)
        if not await seen.check_and_add(message.id):
            log.info("message déjà traité, ignoré: %s", message.id)
            return Response(status_code=200)

        channels = PostgresChannelRegistry(session)
        channel = await channels.get_verified("whatsapp", message.from_number)
        if channel is None:
            # Un numéro inconnu envoie peut-être son code d'appairage.
            if message.kind == "text" and message.text:
                if await _try_pairing(channels, message.text, message.from_number):
                    return Response(status_code=200)
            log.info("numéro non appairé, ignoré")
            return Response(status_code=200)
        user_id = channel.user_id

    # ACK immédiat, mise en file : au-delà de 5 s l'émetteur retente, et un
    # BackgroundTask mourrait avec le processus — la file survit à un redémarrage.
    await request.app.state.arq_pool.enqueue_job(
        "handle_message_job",
        user_id,
        {
            "id": message.id,
            "channel": "whatsapp",
            "from_id": message.from_number,
            "kind": message.kind,
            "text": message.text,
            "media_url": message.media_url,
        },
        _queue_name=Priority.INTERACTIVE.queue,
    )
    return Response(status_code=200)


async def _try_pairing(channels: PostgresChannelRegistry, text: str, number: str) -> bool:
    """Tente d'interpréter le message comme un code d'appairage à 6 chiffres."""
    candidate = text.strip().replace(" ", "")
    if not (candidate.isdigit() and len(candidate) == 6):
        return False
    try:
        await channels.redeem(candidate, "whatsapp", number)
    except Exception as exc:  # PairingError et dérivés
        log.info("appairage refusé: %s", exc)
        return False
    log.info("canal WhatsApp appairé")
    return True
