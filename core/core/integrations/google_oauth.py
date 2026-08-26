"""OAuth Google — obtention et rafraîchissement des jetons.
Deux pièges coûteux sont traités ici :
- `access_type=offline` et `prompt=consent` sont indispensables à l'autorisation
  initiale. Sans eux, Google ne délivre pas de `refresh_token`, et l'intégration
  meurt silencieusement une heure plus tard.
- Un `refresh_token` révoqué (mot de passe changé, accès retiré, app restée en
  mode « Testing » plus de 7 jours) renvoie `invalid_grant`. Ce n'est pas une
  erreur transitoire : il faut une reconnexion de l'utilisateur, et le dire.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import urlencode
from .http import HttpError, Response, Transport, request_with_retry
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/contacts.readonly",
)
#: On rafraîchit un peu avant l'expiration : un jeton valide 20 s au moment de
#: l'appel aura expiré au moment où Google le traite.
EXPIRY_SKEW_SECONDS = 120
class ReauthRequired(RuntimeError):
    """Le refresh token n'est plus valide : seul l'utilisateur peut débloquer."""
@dataclass(frozen=True)
class GoogleCredentials:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.expires_at - EXPIRY_SKEW_SECONDS
class CredentialStore(Protocol):
    """Persistance des jetons. L'implémentation réelle chiffre via crypto.py."""
    async def load(self, user_id: str) -> GoogleCredentials: ...
    async def save(self, user_id: str, credentials: GoogleCredentials) -> None: ...
def build_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",  # sans ceci, aucun refresh_token
        "prompt": "consent",  # force sa réémission même si déjà autorisé
        "include_granted_scopes": "true",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"
async def exchange_code(
    transport: Transport,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    now: float | None = None,
) -> GoogleCredentials:
    response = await request_with_retry(
        transport,
        "POST",
        TOKEN_ENDPOINT,
        json={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    return _credentials_from(response, refresh_token=None, now=now)
async def refresh_access_token(
    transport: Transport,
    credentials: GoogleCredentials,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> GoogleCredentials:
    try:
        response = await request_with_retry(
            transport,
            "POST",
            TOKEN_ENDPOINT,
            json={
                "refresh_token": credentials.refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
    except HttpError as exc:
        if exc.status_code in (400, 401) and "invalid_grant" in exc.detail:
            raise ReauthRequired(
                "L'autorisation Google a été révoquée ou a expiré. "
                "Une reconnexion de l'utilisateur est nécessaire."
            ) from exc
        raise
    # Google ne renvoie pas le refresh_token lors d'un refresh : on conserve
    # celui qu'on a, sous peine de perdre l'accès au tour suivant.
    return _credentials_from(response, refresh_token=credentials.refresh_token, now=now)
async def ensure_fresh(
    transport: Transport,
    store: CredentialStore,
    user_id: str,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> GoogleCredentials:
    """Renvoie des identifiants utilisables, en rafraîchissant si nécessaire."""
    credentials = await store.load(user_id)
    if not credentials.is_expired(now):
        return credentials
    refreshed = await refresh_access_token(
        transport, credentials, client_id, client_secret, now
    )
    await store.save(user_id, refreshed)
    return refreshed
def _credentials_from(
    response: Response, refresh_token: str | None, now: float | None
) -> GoogleCredentials:
    body = response.json() or {}
    now = now if now is not None else time.time()
    token = body.get("refresh_token") or refresh_token
    if not token:
        raise ReauthRequired(
            "Google n'a pas fourni de refresh_token. Vérifie access_type=offline "
            "et prompt=consent dans l'URL d'autorisation."
        )
    return GoogleCredentials(
        access_token=body["access_token"],
        refresh_token=token,
        expires_at=now + float(body.get("expires_in", 3600)),
    )
def with_access_token(credentials: GoogleCredentials, access_token: str) -> GoogleCredentials:
    return replace(credentials, access_token=access_token)
