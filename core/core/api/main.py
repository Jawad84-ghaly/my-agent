"""Application FastAPI — la porte d'entrée de tous les canaux.

Le gateway ne contient aucune logique agentique : il authentifie, normalise,
met en file, et rend la main. Tout le raisonnement vit dans le graphe.
"""

from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response

from .webhooks import SeenCache, WebhookRejected, normalize_evolution, verify_signature

log = logging.getLogger("nova.api")

app = FastAPI(title="Nova Core", version="0.1.0")
seen = SeenCache()

WHATSAPP_SECRET = os.environ.get("NOVA_WHATSAPP_WEBHOOK_SECRET", "")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background: BackgroundTasks,
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

    if not seen.check_and_add(message.id):
        log.info("message déjà traité, ignoré: %s", message.id)
        return Response(status_code=200)

    channel = await get_verified_channel(message.from_number)
    if channel is None:
        log.info("numéro non appairé, ignoré")
        return Response(status_code=200)

    # ACK immédiat, traitement asynchrone : au-delà de 5 s, l'émetteur retente.
    background.add_task(handle_message, channel["user_id"], message)
    return Response(status_code=200)


async def get_verified_channel(number: str) -> dict | None:
    """Cherche un canal appairé pour ce numéro.

    À câbler sur la table `channels`. L'appairage se fait par code à 6 chiffres
    généré dans le dashboard puis envoyé au bot.
    """
    raise NotImplementedError("à câbler sur la table channels")


async def handle_message(user_id: str, message) -> None:
    """Point d'entrée du pipeline : normalisation, graphe, réponse.

    À câbler sur la file ARQ/Celery en production — un BackgroundTask meurt avec
    le processus, ce qui est acceptable en développement seulement.
    """
    raise NotImplementedError("à câbler sur le worker")
