"""Dédup pour les opérations sans id imposable côté client.

L'API Calendar Google accepte un `id` choisi par l'appelant : un retry après
timeout hite un 409 traité comme un succès (`providers/google_calendar.py`).
Ni Gmail (`drafts.create`/`drafts.send`) ni Microsoft Graph (`/events`,
`/drafts`... aucun n'accepte d'id client) n'ont cet équivalent — Google comme
Microsoft génèrent toujours un nouvel identifiant côté serveur. `IdempotencyStore`
porte cette garantie à leur place : la clé dérivée par le registre d'outils est
vérifiée avant l'appel HTTP et enregistrée après, ce que ferait un en-tête
`Idempotency-Key` si ces API en proposaient un.

`PostgresIdempotencyStore` (`db/repositories.py`, table `idempotency_records`)
est l'implémentation durable ; `InMemoryIdempotencyStore` ici sert les tests et
les providers en mémoire.
"""

from __future__ import annotations

from typing import Protocol


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def put(self, key: str, result_id: str) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._seen.get(key)

    async def put(self, key: str, result_id: str) -> None:
        self._seen[key] = result_id
