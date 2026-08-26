import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest

from core.api.oauth import OAuthConfig, StateError, handle_callback, sign_state, verify_state
from core.channels import ChannelRegistry, PairingError
from core.integrations.google_oauth import GoogleCredentials
from core.integrations.http import Response
from core.security.crypto import CryptoError, generate_key
from core.store import EncryptedCredentialStore, InMemoryBackend, UnknownIntegration
from conftest import FakeTransport, always

KEY = base64.urlsafe_b64decode(generate_key())
NOW = 1_800_000_000.0
T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def make_store(backend=None) -> EncryptedCredentialStore:
    return EncryptedCredentialStore(backend or InMemoryBackend(), KEY)


# --- store chiffré ---------------------------------------------------------

def test_roundtrip_through_the_store():
    store = make_store()
    creds = GoogleCredentials("at-1", "rt-1", NOW + 3600)
    run(store.save("u1", creds))
    loaded = run(store.load("u1"))
    assert loaded.access_token == "at-1"
    assert loaded.refresh_token == "rt-1"


def test_tokens_are_never_written_in_clear():
    backend = InMemoryBackend()
    store = make_store(backend)
    run(store.save("u1", GoogleCredentials("at-secret", "rt-secret", NOW)))
    blob = repr(backend.rows["u1:google"]).encode()
    assert b"at-secret" not in blob
    assert b"rt-secret" not in blob


def test_expiry_stays_readable_without_decrypting():
    backend = InMemoryBackend()
    run(make_store(backend).save("u1", GoogleCredentials("at", "rt", NOW + 42)))
    assert backend.rows["u1:google"]["expires_at"] == NOW + 42


def test_row_of_another_user_cannot_be_decrypted():
    """Un accès en écriture à la base ne permet pas de déplacer un jeton."""
    backend = InMemoryBackend()
    store = make_store(backend)
    run(store.save("victime", GoogleCredentials("at", "rt", NOW)))
    backend.rows["attaquant:google"] = dict(backend.rows["victime:google"])
    with pytest.raises(CryptoError):
        run(store.load("attaquant"))


def test_missing_integration_is_explicit():
    with pytest.raises(UnknownIntegration, match="autoriser"):
        run(make_store().load("inconnu"))


def test_exists_reports_presence():
    store = make_store()
    assert run(store.exists("u1")) is False
    run(store.save("u1", GoogleCredentials("at", "rt", NOW)))
    assert run(store.exists("u1")) is True


# --- state OAuth -----------------------------------------------------------

def test_state_roundtrip():
    state = sign_state("u1", "secret", NOW)
    assert verify_state(state, "secret", NOW + 10) == "u1"


def test_tampered_state_is_rejected():
    state = sign_state("u1", "secret", NOW)
    body, _, sig = state.partition(".")
    forged = sign_state("attaquant", "secret", NOW).split(".")[0] + "." + sig
    with pytest.raises(StateError, match="signature"):
        verify_state(forged, "secret", NOW)


def test_state_signed_with_another_secret_is_rejected():
    with pytest.raises(StateError):
        verify_state(sign_state("u1", "autre", NOW), "secret", NOW)


def test_expired_state_is_rejected():
    state = sign_state("u1", "secret", NOW)
    with pytest.raises(StateError, match="expiré"):
        verify_state(state, "secret", NOW + 601)


def test_malformed_state_is_rejected():
    for bad in ("", "sans-point", "aaaa.bbbb"):
        with pytest.raises(StateError):
            verify_state(bad, "secret", NOW)


# --- callback --------------------------------------------------------------

def config() -> OAuthConfig:
    return OAuthConfig("cid", "csecret", "https://nova.fr/cb", "state-secret")


def token_response():
    return always(Response(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}))


def test_callback_stores_credentials():
    store = make_store()
    state = sign_state("u1", "state-secret", NOW)
    user = run(handle_callback(config(), FakeTransport(token_response()), store, "code", state, "u1", NOW))
    assert user == "u1"
    assert run(store.load("u1")).access_token == "at-1"


def test_callback_refuses_account_confusion():
    """Sans ce contrôle, on connecte le Google d'un attaquant au Nova de la victime."""
    store = make_store()
    state = sign_state("attaquant", "state-secret", NOW)
    with pytest.raises(StateError, match="ne correspond pas"):
        run(handle_callback(config(), FakeTransport(token_response()), store, "code", state, "victime", NOW))


def test_callback_does_not_store_on_invalid_state():
    store = make_store()
    with pytest.raises(StateError):
        run(handle_callback(config(), FakeTransport(token_response()), store, "code", "bidon", None, NOW))
    assert run(store.exists("u1")) is False


# --- appairage des canaux --------------------------------------------------

def test_pairing_registers_the_channel():
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    channel = reg.redeem(code, "whatsapp", "33612345678", T0)
    assert channel.user_id == "u1"
    assert reg.get_verified("whatsapp", "33612345678") is channel


def test_unknown_number_is_not_verified():
    """Le webhook doit pouvoir ignorer un inconnu en silence."""
    assert ChannelRegistry().get_verified("whatsapp", "33699999999") is None


def test_code_is_single_use():
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    reg.redeem(code, "whatsapp", "33612345678", T0)
    with pytest.raises(PairingError):
        reg.redeem(code, "whatsapp", "33600000000", T0)


def test_expired_code_is_refused():
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    with pytest.raises(PairingError):
        reg.redeem(code, "whatsapp", "33612345678", T0 + timedelta(minutes=11))


def test_wrong_code_is_refused():
    reg = ChannelRegistry()
    reg.issue_code("u1", T0)
    with pytest.raises(PairingError, match="invalide"):
        reg.redeem("000000", "whatsapp", "33612345678", T0)


def test_brute_force_burns_the_code():
    """100 000 combinaisons se testent vite : le code doit mourir avant."""
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    for _ in range(6):
        with pytest.raises(PairingError):
            reg.redeem("999999", "whatsapp", "33612345678", T0)
    with pytest.raises(PairingError):
        reg.redeem(code, "whatsapp", "33612345678", T0)


def test_issuing_a_new_code_invalidates_the_previous_one():
    reg = ChannelRegistry()
    first = reg.issue_code("u1", T0)
    reg.issue_code("u1", T0)
    with pytest.raises(PairingError):
        reg.redeem(first, "whatsapp", "33612345678", T0)


def test_code_format():
    code = ChannelRegistry().issue_code("u1", T0)
    assert len(code) == 6 and code.isdigit()


def test_spaces_in_submitted_code_are_tolerated():
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    spaced = f"{code[:3]} {code[3:]}"
    assert reg.redeem(spaced, "whatsapp", "33612345678", T0).user_id == "u1"


def test_revoked_channel_stops_being_verified():
    reg = ChannelRegistry()
    code = reg.issue_code("u1", T0)
    reg.redeem(code, "whatsapp", "33612345678", T0)
    assert reg.revoke("whatsapp", "33612345678") is True
    assert reg.get_verified("whatsapp", "33612345678") is None
