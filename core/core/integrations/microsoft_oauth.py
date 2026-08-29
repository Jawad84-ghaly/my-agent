"""OAuth Microsoft (Graph) — même structure que google_oauth.py, endpoints différents.

Deux différences qui ne sont pas devinables depuis la documentation Microsoft :

- **Le tenant `common`** accepte comptes personnels *et* professionnels/scolaires.
  Un tenant dédié (l'id d'une organisation) restreindrait l'accès aux seuls
  comptes de cette organisation — sans intérêt pour un assistant personnel.
- **`offline_access` remplace `access_type=offline`.** Sans ce scope explicite
  dans la demande d'autorisation, Microsoft ne délivre pas de refresh_token,
  exactement le même piège que Google mais un mécanisme différent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from .http import HttpError, Response, Transport, request_with_retry

TENANT = "common"
AUTH_ENDPOINT = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
SCOPES = (
    "offline_access",
    "https://graph.microsoft.com/Calendars.ReadWrite",
)
#: On rafraîchit un peu avant l'expiration, comme pour Google.
EXPIRY_SKEW_SECONDS = 120


class ReauthRequired(RuntimeError):
    """Le refresh token n'est plus valide : seul l'utilisateur peut débloquer."""


@dataclass(frozen=True)
class MicrosoftCredentials:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.expires_at - EXPIRY_SKEW_SECONDS


class CredentialStore(Protocol):
    """Persistance des jetons. L'implémentation réelle chiffre via crypto.py."""

    async def load(self, user_id: str) -> MicrosoftCredentials: ...

    async def save(self, user_id: str, credentials: MicrosoftCredentials) -> None: ...


def build_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(
    transport: Transport,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    now: float | None = None,
) -> MicrosoftCredentials:
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
            "scope": " ".join(SCOPES),
        },
    )
    return _credentials_from(response, refresh_token=None, now=now)


async def refresh_access_token(
    transport: Transport,
    credentials: MicrosoftCredentials,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> MicrosoftCredentials:
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
                "scope": " ".join(SCOPES),
            },
        )
    except HttpError as exc:
        if exc.status_code in (400, 401) and "invalid_grant" in exc.detail:
            raise ReauthRequired(
                "L'autorisation Microsoft a été révoquée ou a expiré. "
                "Une reconnexion de l'utilisateur est nécessaire."
            ) from exc
        raise
    # Microsoft peut ne pas renvoyer de nouveau refresh_token : on conserve
    # celui qu'on a, comme pour Google.
    return _credentials_from(response, refresh_token=credentials.refresh_token, now=now)


async def ensure_fresh(
    transport: Transport,
    store: CredentialStore,
    user_id: str,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> MicrosoftCredentials:
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
) -> MicrosoftCredentials:
    body = response.json() or {}
    now = now if now is not None else time.time()
    token = body.get("refresh_token") or refresh_token
    if not token:
        raise ReauthRequired(
            "Microsoft n'a pas fourni de refresh_token. Vérifie que le scope "
            "offline_access est bien demandé dans l'URL d'autorisation."
        )
    return MicrosoftCredentials(
        access_token=body["access_token"],
        refresh_token=token,
        expires_at=now + float(body.get("expires_in", 3600)),
    )
