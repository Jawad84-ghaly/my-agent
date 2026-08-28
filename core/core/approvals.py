"""Validations en attente — l'état qui survit entre deux messages.

Quand l'exécuteur suspend un plan devant une action sortante, il faut conserver
de quoi reprendre : le plan, les résultats déjà obtenus, et la tâche à valider.
Ce registre porte cet état, indexé par fil de conversation.

Deux règles qui évitent des dégâts réels :

- **Une seule validation en attente par fil.** Sinon un « ok » ambigu pourrait
  valider une action que l'utilisateur croyait abandonnée.
- **Expiration à 30 minutes.** Un « ok » tapé une heure plus tard répond à autre
  chose. L'agent ne relance pas, et la validation périmée est refusée.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .gate import APPROVAL_TTL, is_expired
from .planning import Task


@dataclass
class PendingApproval:
    thread_id: str
    user_id: str
    task: Task
    plan: list[Task]
    results: dict[str, Any]
    completed: list[str]
    summary: str
    requested_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return is_expired(self.requested_at, now)


@dataclass
class ApprovalRegistry:
    """En mémoire ici ; `PostgresApprovalRegistry` (même interface) en production.

    Les méthodes sont `async` — sans I/O ici — pour que `Pipeline` puisse
    recevoir indifféremment l'un ou l'autre registre sans rien savoir de
    lequel des deux il tient.
    """

    pending: dict[str, PendingApproval] = field(default_factory=dict)

    async def put(self, approval: PendingApproval) -> None:
        # Remplace toute validation antérieure sur ce fil : une seule à la fois.
        self.pending[approval.thread_id] = approval

    async def get(self, thread_id: str, now: datetime | None = None) -> PendingApproval | None:
        approval = self.pending.get(thread_id)
        if approval is None:
            return None
        if approval.expired(now):
            del self.pending[thread_id]
            return None
        return approval

    async def pop(self, thread_id: str, now: datetime | None = None) -> PendingApproval | None:
        approval = await self.get(thread_id, now)
        if approval is not None:
            del self.pending[thread_id]
        return approval

    async def purge(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        stale = [tid for tid, a in self.pending.items() if a.expired(now)]
        for tid in stale:
            del self.pending[tid]
        return len(stale)


def ttl_minutes() -> int:
    return int(APPROVAL_TTL.total_seconds() // 60)
