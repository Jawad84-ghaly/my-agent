"""Persistance des jetons OAuth, chiffrés au repos.
`crypto.py` fournissait les primitives ; il manquait la couche qui s'en sert.
`EncryptedCredentialStore` implémente le protocole `CredentialStore` attendu par
`google_oauth.ensure_fresh`, en chiffrant systématiquement avant écriture.
Le backend est volontairement abstrait (`KeyValueBackend`) : en test c'est un
dictionnaire, en production la table `integrations`. Le chiffrement est le même
dans les deux cas — on ne teste pas un chemin différent de celui qui tourne.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol
from .integrations.google_oauth import GoogleCredentials
from .security.crypto import build_aad, decrypt_token, encrypt_token
class UnknownIntegration(KeyError):
    """Aucune intégration enregistrée pour cet utilisateur et ce fournisseur."""
class KeyValueBackend(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def put(self, key: str, value: dict) -> None: ...
@dataclass
class InMemoryBackend:
    rows: dict[str, dict] = field(default_factory=dict)
    async def get(self, key: str) -> dict | None:
        return self.rows.get(key)
    async def put(self, key: str, value: dict) -> None:
        self.rows[key] = value
@dataclass
class EncryptedCredentialStore:
    """Stocke access et refresh tokens chiffrés, expiration en clair.
    L'expiration reste lisible pour éviter de déchiffrer à chaque vérification
    de fraîcheur — elle n'est pas un secret, et la déchiffrer en boucle ferait
    passer la clé maîtresse par la mémoire bien plus souvent que nécessaire.
    """
    backend: KeyValueBackend
    key: bytes
    provider: str = "google"
    def _row_key(self, user_id: str) -> str:
        return f"{user_id}:{self.provider}"
    async def load(self, user_id: str) -> GoogleCredentials:
        row = await self.backend.get(self._row_key(user_id))
        if row is None:
            raise UnknownIntegration(
                f"Aucune intégration {self.provider} pour {user_id}. "
                "L'utilisateur doit d'abord autoriser l'accès."
            )
        aad = build_aad(user_id, self.provider)
        return GoogleCredentials(
            access_token=decrypt_token(row["access_token_enc"], aad, self.key),
            refresh_token=decrypt_token(row["refresh_token_enc"], aad, self.key),
            expires_at=row["expires_at"],
        )
    async def save(self, user_id: str, credentials: GoogleCredentials) -> None:
        aad = build_aad(user_id, self.provider)
        await self.backend.put(
            self._row_key(user_id),
            {
                "user_id": user_id,
                "provider": self.provider,
                "access_token_enc": encrypt_token(credentials.access_token, aad, self.key),
                "refresh_token_enc": encrypt_token(credentials.refresh_token, aad, self.key),
                "expires_at": credentials.expires_at,
            },
        )
    async def exists(self, user_id: str) -> bool:
        return await self.backend.get(self._row_key(user_id)) is not None
