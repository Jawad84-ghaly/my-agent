"""Tests du gateway FastAPI (`core.api.main`), sans réseau ni Redis réel.

`HttpxTransport.request` est monkeypatché plutôt qu'injecté : les routes
OAuth l'instancient elles-mêmes (une connexion par requête, pas un client
partagé), donc c'est le point d'accroche disponible pour éviter le réseau.
Le pool ARQ, lui, est un simple mock posé sur `app.state` — enqueuer un job
n'a pas besoin d'un vrai Redis pour être vérifié.
"""

import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

import core.api.main as main
from core.db.repositories import PostgresCredentialStore
from core.db.session import create_all, make_engine, make_session_factory
from core.integrations import http as http_module
from core.security.crypto import generate_key, load_master_key

STATE_SECRET = "state-secret"
START_SECRET = "start-secret"
ADMIN_SECRET = "admin-secret"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(main, "OAUTH_STATE_SECRET", STATE_SECRET)
    monkeypatch.setattr(main, "OAUTH_START_SECRET", START_SECRET)
    monkeypatch.setattr(main, "ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setattr(main, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(main, "GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(main, "GOOGLE_REDIRECT_URI", "https://nova.example/oauth/google/callback")
    monkeypatch.setattr(main, "MICROSOFT_CLIENT_ID", "ms-client-id")
    monkeypatch.setattr(main, "MICROSOFT_CLIENT_SECRET", "ms-client-secret")
    monkeypatch.setattr(
        main, "MICROSOFT_REDIRECT_URI", "https://nova.example/oauth/microsoft/callback"
    )
    monkeypatch.setenv("NOVA_MASTER_KEY", generate_key())


async def _with_app(body):
    """Base neuve et pool ARQ mocké par test, montés sur l'app partagée."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    main.app.state.session_factory = make_session_factory(engine)
    main.app.state.arq_pool = AsyncMock()
    try:
        transport = ASGITransport(app=main.app)
        async with AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            await body(client)
    finally:
        await engine.dispose()


# --- CORS (voir app/, l'app web) ----------------------------------------


def test_cors_origins_defaults_to_wildcard_when_unset():
    assert main._parse_cors_origins("") == ["*"]


def test_cors_origins_splits_a_comma_separated_list():
    assert main._parse_cors_origins("https://a.example, https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


# --- santé -------------------------------------------------------------


def test_health():
    async def body(client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    run(_with_app(body))


# --- démarrage du flow Google --------------------------------------------


def test_start_rejects_wrong_key():
    async def body(client):
        r = await client.get("/oauth/google/start", params={"user_id": "u1", "key": "wrong"})
        assert r.status_code == 403

    run(_with_app(body))


def test_start_rejects_missing_secret_even_with_empty_key(monkeypatch):
    """Secret non configuré : refuser plutôt que traiter une clé vide comme valide."""
    monkeypatch.setattr(main, "OAUTH_START_SECRET", "")

    async def body(client):
        r = await client.get("/oauth/google/start", params={"user_id": "u1", "key": ""})
        assert r.status_code == 403

    run(_with_app(body))


def test_start_redirects_with_signed_state():
    async def body(client):
        r = await client.get(
            "/oauth/google/start", params={"user_id": "u1", "key": START_SECRET}
        )
        assert r.status_code in (302, 307)
        location = r.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        qs = parse_qs(urlparse(location).query)
        assert qs["client_id"] == ["client-id"]
        assert qs["access_type"] == ["offline"]
        assert qs["prompt"] == ["consent"]
        assert "state" in qs

    run(_with_app(body))


# --- callback --------------------------------------------------------------


def _fake_token_response(monkeypatch):
    async def fake_request(self, method, url, *, headers=None, params=None, json=None):
        return http_module.Response(
            200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
        )

    monkeypatch.setattr(http_module.HttpxTransport, "request", fake_request)


def test_callback_persists_encrypted_credentials(monkeypatch):
    _fake_token_response(monkeypatch)

    async def body(client):
        start = await client.get(
            "/oauth/google/start", params={"user_id": "u1", "key": START_SECRET}
        )
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        r = await client.get("/oauth/google/callback", params={"code": "authcode", "state": state})
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        async with main.app.state.session_factory() as session:
            store = PostgresCredentialStore(session, key=load_master_key())
            creds = await store.load("u1")
            assert creds.access_token == "AT"
            assert creds.refresh_token == "RT"

    run(_with_app(body))


def test_callback_rejects_forged_state(monkeypatch):
    _fake_token_response(monkeypatch)

    async def body(client):
        r = await client.get(
            "/oauth/google/callback", params={"code": "authcode", "state": "not-a-real-state"}
        )
        assert r.status_code == 400

    run(_with_app(body))


def test_callback_rejects_state_signed_with_a_different_secret(monkeypatch):
    """Un state d'un autre déploiement (secret différent) ne doit pas être accepté."""
    _fake_token_response(monkeypatch)
    from core.api.oauth import sign_state

    forged = sign_state("u1", "some-other-secret")

    async def body(client):
        r = await client.get(
            "/oauth/google/callback", params={"code": "authcode", "state": forged}
        )
        assert r.status_code == 400

    run(_with_app(body))


# --- flow Microsoft, même garde-fous que Google ----------------------------


def test_microsoft_start_rejects_wrong_key():
    async def body(client):
        r = await client.get("/oauth/microsoft/start", params={"user_id": "u1", "key": "wrong"})
        assert r.status_code == 403

    run(_with_app(body))


def test_microsoft_start_redirects_with_signed_state():
    async def body(client):
        r = await client.get(
            "/oauth/microsoft/start", params={"user_id": "u1", "key": START_SECRET}
        )
        assert r.status_code in (302, 307)
        location = r.headers["location"]
        assert location.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
        qs = parse_qs(urlparse(location).query)
        assert qs["client_id"] == ["ms-client-id"]
        assert "offline_access" in qs["scope"][0]
        assert "state" in qs

    run(_with_app(body))


def test_microsoft_callback_persists_encrypted_credentials(monkeypatch):
    _fake_token_response(monkeypatch)

    async def body(client):
        start = await client.get(
            "/oauth/microsoft/start", params={"user_id": "u1", "key": START_SECRET}
        )
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        r = await client.get(
            "/oauth/microsoft/callback", params={"code": "authcode", "state": state}
        )
        assert r.status_code == 200

        async with main.app.state.session_factory() as session:
            store = PostgresCredentialStore(session, key=load_master_key(), provider="microsoft")
            creds = await store.load("u1")
            assert creds.access_token == "AT"
            assert creds.refresh_token == "RT"

    run(_with_app(body))


def test_microsoft_callback_rejects_forged_state(monkeypatch):
    _fake_token_response(monkeypatch)

    async def body(client):
        r = await client.get(
            "/oauth/microsoft/callback", params={"code": "authcode", "state": "not-a-real-state"}
        )
        assert r.status_code == 400

    run(_with_app(body))


# --- code d'appairage (admin) -------------------------------------------


def test_admin_pairing_code_rejects_wrong_key():
    async def body(client):
        r = await client.post(
            "/admin/pairing-code", params={"user_id": "u1", "key": "wrong"}
        )
        assert r.status_code == 403

    run(_with_app(body))


def test_admin_pairing_code_rejects_missing_secret_even_with_empty_key(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "")

    async def body(client):
        r = await client.post("/admin/pairing-code", params={"user_id": "u1", "key": ""})
        assert r.status_code == 403

    run(_with_app(body))


def test_admin_pairing_code_issues_a_redeemable_code():
    async def body(client):
        r = await client.post(
            "/admin/pairing-code", params={"user_id": "u1", "key": ADMIN_SECRET}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["expires_in_minutes"] == 10
        code = data["code"]
        assert code.isdigit() and len(code) == 6

        from core.db.repositories import PostgresChannelRegistry

        async with main.app.state.session_factory() as session:
            channels = PostgresChannelRegistry(session)
            channel = await channels.redeem(code, "whatsapp", "33612345678")
            assert channel.user_id == "u1"

    run(_with_app(body))


# --- webhook WhatsApp --------------------------------------------------


def test_webhook_pairs_then_enqueues_and_dedupes(monkeypatch):
    import hashlib
    import hmac
    import json

    monkeypatch.setattr(main, "WHATSAPP_SECRET", "wa-secret")

    def sign(body: bytes) -> str:
        return "sha256=" + hmac.new(b"wa-secret", body, hashlib.sha256).hexdigest()

    async def body(client):
        from core.db.repositories import PostgresChannelRegistry

        async with main.app.state.session_factory() as session:
            reg = PostgresChannelRegistry(session)
            code = await reg.issue_code("u1")
            await session.commit()

        pairing = json.dumps(
            {
                "data": {
                    "key": {"id": "MSG1", "remoteJid": "33612345678@s.whatsapp.net"},
                    "message": {"conversation": code},
                }
            }
        ).encode()
        r1 = await client.post(
            "/webhooks/whatsapp", content=pairing, headers={"X-Hub-Signature-256": sign(pairing)}
        )
        assert r1.status_code == 200
        main.app.state.arq_pool.enqueue_job.assert_not_awaited()

        text_msg = json.dumps(
            {
                "data": {
                    "key": {"id": "MSG2", "remoteJid": "33612345678@s.whatsapp.net"},
                    "message": {"conversation": "salut"},
                }
            }
        ).encode()
        r2 = await client.post(
            "/webhooks/whatsapp", content=text_msg, headers={"X-Hub-Signature-256": sign(text_msg)}
        )
        assert r2.status_code == 200
        main.app.state.arq_pool.enqueue_job.assert_awaited_once_with(
            "handle_message_job",
            "u1",
            {
                "id": "MSG2",
                "channel": "whatsapp",
                "from_id": "33612345678",
                "kind": "text",
                "text": "salut",
                "media_url": None,
            },
            _queue_name="nova:p0",
        )

        # rejeu du même message : pas de deuxième job.
        r3 = await client.post(
            "/webhooks/whatsapp", content=text_msg, headers={"X-Hub-Signature-256": sign(text_msg)}
        )
        assert r3.status_code == 200
        assert main.app.state.arq_pool.enqueue_job.await_count == 1

    run(_with_app(body))


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(main, "WHATSAPP_SECRET", "wa-secret")

    async def body(client):
        r = await client.post(
            "/webhooks/whatsapp", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"}
        )
        assert r.status_code == 200
        main.app.state.arq_pool.enqueue_job.assert_not_awaited()

    run(_with_app(body))
