"""Chiffrement des tokens OAuth au repos.

Un refresh token Google donne accès à l'agenda et à la messagerie de l'utilisateur
sans expiration : il ne doit jamais se trouver en clair dans la base ni dans un
`.env` versionné.

L'AAD (`user_id:provider`) est authentifiée mais pas chiffrée : elle interdit de
rejouer le blob d'un utilisateur dans la ligne d'un autre. Un attaquant ayant un
accès en écriture à la base ne peut pas déplacer un token d'un compte à l'autre.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_ENV = "NOVA_MASTER_KEY"  # 32 octets encodés en base64url


class CryptoError(RuntimeError):
    pass


def load_master_key() -> bytes:
    """Charge la clé maîtresse. En production, elle vient de Vault ou d'un KMS."""
    raw = os.environ.get(KEY_ENV)
    if not raw:
        raise CryptoError(
            f"{KEY_ENV} absent. Génère une clé avec "
            "`python -m core.security.crypto --generate`."
        )
    key = base64.urlsafe_b64decode(raw)
    if len(key) != 32:
        raise CryptoError(f"{KEY_ENV} doit faire 32 octets (AES-256), reçu {len(key)}")
    return key


def build_aad(user_id: str, provider: str) -> bytes:
    return f"{user_id}:{provider}".encode()


def encrypt_token(plaintext: str, aad: bytes, key: bytes | None = None) -> bytes:
    key = key or load_master_key()
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return nonce + sealed


def decrypt_token(blob: bytes, aad: bytes, key: bytes | None = None) -> str:
    key = key or load_master_key()
    if len(blob) <= NONCE_BYTES:
        raise CryptoError("blob chiffré tronqué")
    nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, aad).decode()
    except InvalidTag as exc:
        raise CryptoError(
            "déchiffrement refusé : clé incorrecte, données altérées, "
            "ou blob appartenant à un autre utilisateur/provider"
        ) from exc


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--generate" in sys.argv:
        print(generate_key())
    else:
        print(f"usage: python -m core.security.crypto --generate")
