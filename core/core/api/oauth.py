"""Routes OAuth — le bout manquant entre l'URL d'autorisation et le store.
Le paramètre `state` est le seul rempart contre le CSRF sur ce flux : sans lui,
un attaquant peut faire connecter *son* compte Google au compte Nova de la
victime, et lire ensuite tout ce que l'agent y écrit. On le signe donc (HMAC),
on y met l'identité attendue, et on le fait expirer.
"""
from __future__ import annotations
import base64
import hmac
import hashlib
import json
import time
from dataclasses import dataclass
STATE_TTL_SECONDS = 600
class StateError(RuntimeError):
    """State absent, altéré, expiré, ou destiné à un autre utilisateur."""
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
def sign_state(user_id: str, secret: str, now: float | None = None) -> str:
    payload = {
        "uid": user_id,
        "iat": int(now if now is not None else time.time()),
        "nonce": _b64e(hashlib.sha256(f"{user_id}{time.time_ns()}".encode()).digest()[:12]),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{signature}"
def verify_state(state: str, secret: str, now: float | None = None) -> str:
    """Renvoie l'user_id porté par le state. Lève StateError sinon."""
    if not state or "." not in state:
        raise StateError("state absent ou malformé")
    body, _, signature = state.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        raise StateError("signature du state invalide")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise StateError("contenu du state illisible") from exc
    now = now if now is not None else time.time()
    if now - float(payload.get("iat", 0)) > STATE_TTL_SECONDS:
        raise StateError("state expiré")
    user_id = payload.get("uid")
    if not user_id:
        raise StateError("state sans identité")
    return user_id
@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    state_secret: str
async def handle_callback(
    config: OAuthConfig,
    transport,
    store,
    code: str,
    state: str,
    session_user_id: str | None = None,
    now: float | None = None,
) -> str:
    """Échange le code contre des jetons et les enregistre chiffrés.
    `session_user_id` est l'utilisateur connecté au moment du retour. S'il ne
    correspond pas au state, on refuse : c'est exactement la signature d'une
    attaque par confusion de compte.
    """
    from ..integrations.google_oauth import exchange_code
    user_id = verify_state(state, config.state_secret, now)
    if session_user_id is not None and session_user_id != user_id:
        raise StateError("le state ne correspond pas à l'utilisateur connecté")
    credentials = await exchange_code(
        transport,
        code,
        config.client_id,
        config.client_secret,
        config.redirect_uri,
        now,
    )
    await store.save(user_id, credentials)
    return user_id
