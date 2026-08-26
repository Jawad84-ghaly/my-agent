import asyncio

import pytest

from core.integrations.google_oauth import (
    GoogleCredentials,
    ReauthRequired,
    build_authorization_url,
    ensure_fresh,
    exchange_code,
    refresh_access_token,
)
from core.integrations.http import Response
from conftest import FakeTransport, always, sequence

CLIENT_ID = "client-123"
CLIENT_SECRET = "secret-456"
NOW = 1_800_000_000.0


def run(coro):
    return asyncio.run(coro)


class MemoryStore:
    def __init__(self, credentials: GoogleCredentials) -> None:
        self.credentials = credentials
        self.saves = 0

    async def load(self, user_id: str) -> GoogleCredentials:
        return self.credentials

    async def save(self, user_id: str, credentials: GoogleCredentials) -> None:
        self.credentials = credentials
        self.saves += 1


# --- URL d'autorisation ----------------------------------------------------

def test_authorization_url_requests_offline_access():
    """Sans access_type=offline, Google ne délivre aucun refresh_token."""
    url = build_authorization_url(CLIENT_ID, "https://nova.fr/cb", "state-1")
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_authorization_url_carries_state_and_scopes():
    url = build_authorization_url(CLIENT_ID, "https://nova.fr/cb", "state-1")
    assert "state=state-1" in url
    assert "calendar.events" in url
    assert "gmail.send" in url


# --- échange du code -------------------------------------------------------

def test_exchange_code_returns_credentials():
    transport = FakeTransport(
        always(Response(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}))
    )
    creds = run(exchange_code(transport, "code-abc", CLIENT_ID, CLIENT_SECRET, "https://nova.fr/cb", NOW))
    assert creds.access_token == "at-1"
    assert creds.expires_at == NOW + 3600


def test_missing_refresh_token_is_reported_clearly():
    """Le symptôme est différé d'une heure ; l'erreur doit être explicite tout de suite."""
    transport = FakeTransport(always(Response(200, {"access_token": "at-1", "expires_in": 3600})))
    with pytest.raises(ReauthRequired, match="access_type=offline"):
        run(exchange_code(transport, "code", CLIENT_ID, CLIENT_SECRET, "https://nova.fr/cb", NOW))


# --- rafraîchissement ------------------------------------------------------

def test_refresh_preserves_the_refresh_token():
    """Google ne le renvoie pas au refresh : le perdre coûte l'accès au tour suivant."""
    transport = FakeTransport(always(Response(200, {"access_token": "at-2", "expires_in": 3600})))
    old = GoogleCredentials("at-1", "rt-1", NOW)
    new = run(refresh_access_token(transport, old, CLIENT_ID, CLIENT_SECRET, NOW))
    assert new.access_token == "at-2"
    assert new.refresh_token == "rt-1"


def test_revoked_grant_asks_for_reconnection():
    transport = FakeTransport(
        always(Response(400, {"error": "invalid_grant", "error_description": "Token revoked"}))
    )
    creds = GoogleCredentials("at-1", "rt-dead", NOW)
    with pytest.raises(ReauthRequired, match="reconnexion"):
        run(refresh_access_token(transport, creds, CLIENT_ID, CLIENT_SECRET, NOW))


def test_server_error_is_not_mistaken_for_revocation():
    """Un 500 est transitoire : ne pas déconnecter l'utilisateur pour rien."""
    transport = FakeTransport(always(Response(500, {"error": {"message": "backend error"}})))
    creds = GoogleCredentials("at-1", "rt-1", NOW)
    with pytest.raises(Exception) as exc:
        run(refresh_access_token(transport, creds, CLIENT_ID, CLIENT_SECRET, NOW))
    assert not isinstance(exc.value, ReauthRequired)


# --- expiration ------------------------------------------------------------

def test_valid_token_is_not_refreshed():
    store = MemoryStore(GoogleCredentials("at-1", "rt-1", NOW + 3600))
    transport = FakeTransport(always(Response(500, None)))
    creds = run(ensure_fresh(transport, store, "u1", CLIENT_ID, CLIENT_SECRET, NOW))
    assert creds.access_token == "at-1"
    assert transport.requests == []  # aucun appel réseau


def test_expired_token_is_refreshed_and_persisted():
    store = MemoryStore(GoogleCredentials("at-1", "rt-1", NOW - 10))
    transport = FakeTransport(always(Response(200, {"access_token": "at-2", "expires_in": 3600})))
    creds = run(ensure_fresh(transport, store, "u1", CLIENT_ID, CLIENT_SECRET, NOW))
    assert creds.access_token == "at-2"
    assert store.saves == 1


def test_token_expiring_within_the_skew_is_refreshed_early():
    """Un jeton valide 20 s a expiré le temps que Google le traite."""
    store = MemoryStore(GoogleCredentials("at-1", "rt-1", NOW + 20))
    transport = FakeTransport(always(Response(200, {"access_token": "at-2", "expires_in": 3600})))
    creds = run(ensure_fresh(transport, store, "u1", CLIENT_ID, CLIENT_SECRET, NOW))
    assert creds.access_token == "at-2"
