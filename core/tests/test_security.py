import hashlib
import hmac
import json
import time

import pytest

from core.api.webhooks import (
    SeenCache,
    WebhookRejected,
    normalize_evolution,
    verify_signature,
)
from core.security.crypto import CryptoError, build_aad, decrypt_token, encrypt_token, generate_key
import base64

KEY = base64.urlsafe_b64decode(generate_key())
SECRET = "s3cr3t"


def sign(body: bytes, timestamp: str | None = None) -> str:
    payload = f"{timestamp}.".encode() + body if timestamp else body
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


# --- chiffrement des tokens ------------------------------------------------

def test_roundtrip():
    aad = build_aad("user-1", "google")
    blob = encrypt_token("refresh-token-abc", aad, KEY)
    assert b"refresh-token-abc" not in blob
    assert decrypt_token(blob, aad, KEY) == "refresh-token-abc"


def test_blob_cannot_be_moved_to_another_user():
    """Un accès en écriture à la base ne doit pas permettre de voler un token."""
    blob = encrypt_token("refresh-token-abc", build_aad("victime", "google"), KEY)
    with pytest.raises(CryptoError):
        decrypt_token(blob, build_aad("attaquant", "google"), KEY)


def test_blob_cannot_be_moved_to_another_provider():
    blob = encrypt_token("tok", build_aad("user-1", "google"), KEY)
    with pytest.raises(CryptoError):
        decrypt_token(blob, build_aad("user-1", "microsoft"), KEY)


def test_tampering_is_detected():
    aad = build_aad("user-1", "google")
    blob = bytearray(encrypt_token("tok", aad, KEY))
    blob[-1] ^= 0x01
    with pytest.raises(CryptoError):
        decrypt_token(bytes(blob), aad, KEY)


def test_truncated_blob_is_rejected():
    with pytest.raises(CryptoError, match="tronqué"):
        decrypt_token(b"short", build_aad("u", "google"), KEY)


def test_nonce_differs_between_encryptions():
    aad = build_aad("user-1", "google")
    assert encrypt_token("tok", aad, KEY) != encrypt_token("tok", aad, KEY)


# --- webhook ---------------------------------------------------------------

def test_valid_signature_passes():
    body = b'{"event":"messages.upsert"}'
    verify_signature(body, sign(body), SECRET)


def test_missing_signature_is_rejected():
    with pytest.raises(WebhookRejected, match="absente"):
        verify_signature(b"{}", "", SECRET)


def test_wrong_signature_is_rejected():
    with pytest.raises(WebhookRejected, match="invalide"):
        verify_signature(b"{}", "deadbeef", SECRET)


def test_sha256_prefix_is_accepted():
    body = b'{"a":1}'
    verify_signature(body, f"sha256={sign(body)}", SECRET)


def test_replayed_request_outside_window_is_rejected():
    body = b'{"a":1}'
    old = str(time.time() - 600)
    with pytest.raises(WebhookRejected, match="hors fenêtre"):
        verify_signature(body, sign(body, old), SECRET, old)


def test_fresh_timestamped_request_passes():
    body = b'{"a":1}'
    now = str(time.time())
    verify_signature(body, sign(body, now), SECRET, now)


# --- normalisation ---------------------------------------------------------

def evolution_payload(**message) -> dict:
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"id": "MSG1", "remoteJid": "33612345678@s.whatsapp.net", "fromMe": False},
            "message": message,
        },
    }


def test_text_message_is_normalized():
    msg = normalize_evolution(evolution_payload(conversation="Bonjour"))
    assert msg.kind == "text"
    assert msg.text == "Bonjour"
    assert msg.from_number == "33612345678"


def test_audio_message_is_normalized():
    msg = normalize_evolution(evolution_payload(audioMessage={"url": "https://x/a.ogg"}))
    assert msg.kind == "audio"
    assert msg.media_url == "https://x/a.ogg"


def test_own_outgoing_message_is_ignored():
    payload = evolution_payload(conversation="coucou")
    payload["data"]["key"]["fromMe"] = True
    assert normalize_evolution(payload) is None


def test_status_events_are_ignored():
    assert normalize_evolution({"event": "messages.update", "data": {}}) is None


def test_unsupported_message_type_is_ignored():
    assert normalize_evolution(evolution_payload(stickerMessage={"url": "x"})) is None


# --- déduplication ---------------------------------------------------------

def test_duplicate_delivery_is_detected():
    cache = SeenCache()
    assert cache.check_and_add("MSG1") is True
    assert cache.check_and_add("MSG1") is False


def test_entries_expire():
    cache = SeenCache(ttl_seconds=10)
    now = time.time()
    cache.check_and_add("MSG1", now)
    assert cache.check_and_add("MSG1", now + 20) is True
