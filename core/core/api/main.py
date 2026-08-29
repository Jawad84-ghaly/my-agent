"""Application FastAPI — la porte d'entrée de tous les canaux.
Le gateway ne contient aucune logique agentique : il authentifie, normalise,
met en file, et rend la main. Tout le raisonnement vit dans le graphe.
"""
from __future__ import annotations
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from ..channels import PAIRING_TTL
from ..db.repositories import PostgresChannelRegistry, PostgresCredentialStore, PostgresSeenCache
from ..db.session import database_url, make_engine, make_session_factory, session_scope
from ..integrations.google_oauth import build_authorization_url
from ..integrations import microsoft_oauth
from ..integrations.http import HttpxTransport
from ..security.crypto import load_master_key
from ..workers import Priority
from .app_channel import router as app_channel_router
from .oauth import OAuthConfig, StateError, handle_callback, sign_state
from .webhooks import WebhookRejected, normalize_evolution, verify_signature
log = logging.getLogger("nova.api")
WHATSAPP_SECRET = os.environ.get("NOVA_WHATSAPP_WEBHOOK_SECRET", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
GOOGLE_CLIENT_ID = os.environ.get("NOVA_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("NOVA_GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("NOVA_GOOGLE_REDIRECT_URI", "")
MICROSOFT_CLIENT_ID = os.environ.get("NOVA_MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("NOVA_MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_REDIRECT_URI = os.environ.get("NOVA_MICROSOFT_REDIRECT_URI", "")
OAUTH_STATE_SECRET = os.environ.get("NOVA_OAUTH_STATE_SECRET", "")
#: Nova n'a pas de login : ce secret tient lieu d'authentification sur
#: `/oauth/google/start`. Sans lui, n'importe qui pourrait lier son propre
#: compte Google à l'identité d'un autre utilisateur Nova via ce endpoint —
#: exactement la confusion de compte que `handle_callback` refuse plus loin.
OAUTH_START_SECRET = os.environ.get("NOVA_OAUTH_START_SECRET", "")
#: Même rôle qu'OAUTH_START_SECRET, pour la seule autre route qui agit au nom
#: d'un user_id arbitraire : émettre un code d'appairage WhatsApp.
ADMIN_SECRET = os.environ.get("NOVA_ADMIN_SECRET", "")


def _oauth_config() -> OAuthConfig:
    return OAuthConfig(
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state_secret=OAUTH_STATE_SECRET,
    )


def _microsoft_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        client_id=MICROSOFT_CLIENT_ID,
        client_secret=MICROSOFT_CLIENT_SECRET,
        redirect_uri=MICROSOFT_REDIRECT_URI,
        state_secret=OAUTH_STATE_SECRET,
    )


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
app.include_router(app_channel_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/oauth/google/start")
async def oauth_google_start(user_id: str, key: str) -> RedirectResponse:
    """Démarre le flow Google pour `user_id`, protégé par le secret partagé."""
    if not OAUTH_START_SECRET or not hmac.compare_digest(key, OAUTH_START_SECRET):
        raise HTTPException(status_code=403, detail="clé invalide")
    state = sign_state(user_id, OAUTH_STATE_SECRET)
    url = build_authorization_url(GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI, state)
    return RedirectResponse(url)


@app.get("/oauth/google/callback")
async def oauth_google_callback(request: Request, code: str, state: str) -> dict[str, str]:
    """Échange le code contre des jetons et les enregistre chiffrés.

    Aucun `session_user_id` à vérifier ici : l'identité vient du state, signé
    et daté par `/oauth/google/start` — c'est ce endpoint-là qu'il faut
    protéger contre la confusion de compte, pas celui-ci.
    """
    transport = HttpxTransport()
    try:
        async with session_scope(request.app.state.session_factory) as session:
            store = PostgresCredentialStore(session, key=load_master_key())
            try:
                user_id = await handle_callback(_oauth_config(), transport, store, code, state)
            except StateError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await transport.aclose()
    log.info("intégration Google connectée pour %s", user_id)
    return {"status": "ok"}


@app.get("/oauth/microsoft/start")
async def oauth_microsoft_start(user_id: str, key: str) -> RedirectResponse:
    """Démarre le flow Microsoft pour `user_id`, même garde que `/oauth/google/start`."""
    if not OAUTH_START_SECRET or not hmac.compare_digest(key, OAUTH_START_SECRET):
        raise HTTPException(status_code=403, detail="clé invalide")
    state = sign_state(user_id, OAUTH_STATE_SECRET)
    url = microsoft_oauth.build_authorization_url(
        MICROSOFT_CLIENT_ID, MICROSOFT_REDIRECT_URI, state
    )
    return RedirectResponse(url)


@app.get("/oauth/microsoft/callback")
async def oauth_microsoft_callback(request: Request, code: str, state: str) -> dict[str, str]:
    """Échange le code contre des jetons Microsoft et les enregistre chiffrés.

    Même state signé qu'un callback Google — c'est lui, pas ce endpoint, qui
    porte la garantie anti-confusion de compte.
    """
    transport = HttpxTransport()
    try:
        async with session_scope(request.app.state.session_factory) as session:
            store = PostgresCredentialStore(session, key=load_master_key(), provider="microsoft")
            try:
                user_id = await handle_callback(
                    _microsoft_oauth_config(),
                    transport,
                    store,
                    code,
                    state,
                    exchange_code=microsoft_oauth.exchange_code,
                )
            except StateError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await transport.aclose()
    log.info("intégration Microsoft connectée pour %s", user_id)
    return {"status": "ok"}


@app.post("/admin/pairing-code")
async def admin_issue_pairing_code(request: Request, user_id: str, key: str) -> dict[str, object]:
    """Émet un code d'appairage WhatsApp à 6 chiffres pour `user_id`.

    Il n'existe nulle part ailleurs de moyen d'obtenir ce premier code — pas de
    dashboard, pas de CLI — donc pas de moyen de rattacher un premier numéro
    WhatsApp à un utilisateur sans passer par ici. Protégé par le même schéma
    que `/oauth/google/start` : un secret d'opérateur, pas une session.
    """
    if not ADMIN_SECRET or not hmac.compare_digest(key, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="clé invalide")
    async with session_scope(request.app.state.session_factory) as session:
        channels = PostgresChannelRegistry(session)
        code = await channels.issue_code(user_id)
    return {"code": code, "expires_in_minutes": int(PAIRING_TTL.total_seconds() // 60)}


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
