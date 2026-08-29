"""Canal `app` — client natif (Android/iOS/Windows), pas WhatsApp.

Deux différences avec le webhook WhatsApp qui justifient un module séparé :

- **Pas d'émetteur asynchrone.** WhatsApp reçoit sa réponse via l'API Evolution,
  poussée par le worker ; une app native attend la réponse sur la même requête
  HTTP qu'elle a envoyée. `/app/messages` met donc le job en file comme
  d'habitude, mais attend son résultat (`Job.result`) au lieu de rendre la main
  tout de suite — `handle_message_job` renvoie déjà le texte de la réponse,
  ARQ n'a besoin de rien de plus pour le lui faire remonter.
- **Pas de session ni de login.** Comme le reste de Nova. `/app/pair` échange
  un code d'appairage (le même mécanisme que WhatsApp, `ChannelRegistry`)
  contre un jeton opaque, montré une seule fois ; `/app/messages` l'exige en
  `Authorization: Bearer`. Le jeton n'est jamais stocké en clair côté serveur
  (`PostgresDeviceTokenStore`), exactement comme un mot de passe.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Request

from ..channels import PairingError
from ..db.repositories import PostgresChannelRegistry, PostgresDeviceTokenStore
from ..db.session import session_scope

log = logging.getLogger("nova.api.app")

#: Le worker traite un message en quelques secondes en pratique (un ou deux
#: appels Anthropic) ; au-delà, mieux vaut rendre une erreur franche que
#: laisser une app mobile avec une requête ouverte indéfiniment.
REPLY_TIMEOUT_SECONDS = 45

router = APIRouter()


@router.post("/app/pair")
async def app_pair(request: Request, code: str) -> dict[str, str]:
    """Échange un code d'appairage contre un jeton d'app, montré une seule fois.

    Même schéma de sécurité que l'appairage WhatsApp (`_try_pairing` dans
    `main.py`) : code à 6 chiffres, usage unique, comparaison à temps
    constant, essais bornés — voir `core/channels.py`.
    """
    external_id = uuid.uuid4().hex
    async with session_scope(request.app.state.session_factory) as session:
        channels = PostgresChannelRegistry(session)
        try:
            channel = await channels.redeem(code, "app", external_id)
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = await PostgresDeviceTokenStore(session).issue(channel.user_id)
    return {"token": token}


@router.post("/app/messages")
async def app_send_message(
    request: Request,
    text: str,
    thread_id: str | None = None,
    authorization: str = Header(default=""),
) -> dict[str, str]:
    """Envoie un message et attend la réponse de Nova sur la même requête."""
    token = _bearer_token(authorization)
    async with session_scope(request.app.state.session_factory) as session:
        user_id = await PostgresDeviceTokenStore(session).resolve(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="jeton invalide")

    message_id = uuid.uuid4().hex
    job = await request.app.state.arq_pool.enqueue_job(
        "handle_message_job",
        user_id,
        {
            "id": message_id,
            "channel": "app",
            "from_id": user_id,
            "kind": "text",
            "text": text,
            "thread_id": thread_id,
        },
    )
    try:
        reply = await job.result(timeout=REPLY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Nova met plus de temps que prévu à répondre, réessaie."
        ) from exc
    return {"reply": reply}


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="en-tête Authorization manquant ou invalide")
    return token
