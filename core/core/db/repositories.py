"""Registres adossés à la base — mêmes interfaces, état durable.

Chaque classe ici remplace un registre en mémoire sans changer sa signature :
`PostgresChannelRegistry` expose `issue_code` / `redeem` / `get_verified` comme
`ChannelRegistry`, en asynchrone. Le pipeline n'a donc rien à savoir de la base.

Les règles de sécurité sont conservées à l'identique — comparaison à temps
constant sur les codes, essais bornés sur *tous* les codes en attente, expiration
des validations — parce qu'un portage qui les relâche silencieusement est pire
qu'un portage absent.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import PendingApproval
from ..channels import MAX_ATTEMPTS, PAIRING_TTL, Channel, PairingError
from ..gate import APPROVAL_TTL
from ..planning import Task
from ..security.crypto import build_aad, decrypt_token, encrypt_token
from .models import Approval, Integration
from .models import Channel as ChannelRow
from .models import PairingCode as PairingRow
from .models import SeenMessage


def _aware(moment: datetime) -> datetime:
    """SQLite rend des datetimes naïfs ; on les traite comme de l'UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass
class PostgresChannelRegistry:
    """Appairage durable. Un redémarrage ne déconnecte plus les canaux."""

    session: AsyncSession

    async def issue_code(self, user_id: str, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        await self._purge(now)
        # Un seul code actif par utilisateur : en émettre un nouveau invalide
        # l'ancien, sinon deux codes valides doublent la surface de force brute.
        await self.session.execute(delete(PairingRow).where(PairingRow.user_id == user_id))

        code = f"{secrets.randbelow(1_000_000):06d}"
        self.session.add(PairingRow(code=code, user_id=user_id, created_at=now))
        await self.session.flush()
        return code

    async def redeem(
        self, submitted: str, kind: str, external_id: str, now: datetime | None = None
    ) -> Channel:
        now = now or datetime.now(timezone.utc)
        await self._purge(now)

        submitted = submitted.strip().replace(" ", "")
        rows = (await self.session.execute(select(PairingRow))).scalars().all()

        match = None
        for row in rows:
            # Comparaison à temps constant sur tous les candidats : sortir au
            # premier écart révélerait le code par mesure du temps de réponse.
            if hmac.compare_digest(row.code, submitted):
                match = row

        if match is None:
            await self._count_failure(rows)
            raise PairingError("Code invalide ou expiré.")

        if match.consumed:
            raise PairingError("Code déjà utilisé.")

        match.attempts += 1
        if match.attempts > MAX_ATTEMPTS:
            await self.session.delete(match)
            raise PairingError("Trop de tentatives. Génère un nouveau code.")

        await self.session.delete(match)
        row = ChannelRow(
            user_id=match.user_id, kind=kind, external_id=external_id, verified_at=now
        )
        self.session.add(row)
        await self.session.flush()
        return Channel(match.user_id, kind, external_id, now)

    async def get_verified(self, kind: str, external_id: str) -> Channel | None:
        row = (
            await self.session.execute(
                select(ChannelRow).where(
                    ChannelRow.kind == kind,
                    ChannelRow.external_id == external_id,
                    ChannelRow.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return Channel(row.user_id, row.kind, row.external_id, _aware(row.verified_at))

    async def revoke(self, kind: str, external_id: str) -> bool:
        row = (
            await self.session.execute(
                select(ChannelRow).where(
                    ChannelRow.kind == kind, ChannelRow.external_id == external_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.active = False
        await self.session.flush()
        return True

    async def _purge(self, now: datetime) -> None:
        await self.session.execute(
            delete(PairingRow).where(PairingRow.created_at < now - PAIRING_TTL)
        )

    async def _count_failure(self, rows: list[PairingRow]) -> None:
        """Un mauvais code coûte un essai à tous les codes en attente.

        Sinon, balayer l'espace des codes ne coûte rien : chaque tentative ne
        pénaliserait que le code visé, jamais celui qu'on cherche.
        """
        for row in rows:
            row.attempts += 1
            if row.attempts > MAX_ATTEMPTS:
                await self.session.delete(row)
        await self.session.flush()


@dataclass
class PostgresApprovalRegistry:
    """Validations en attente, persistées.

    Une validation en mémoire disparaît au redéploiement : l'utilisateur répond
    « ok » à un récapitulatif que l'agent ne reconnaît plus.
    """

    session: AsyncSession

    async def put(self, approval: PendingApproval) -> None:
        # Remplace toute validation antérieure sur ce fil (clé primaire).
        await self.session.execute(
            delete(Approval).where(Approval.thread_id == approval.thread_id)
        )
        self.session.add(
            Approval(
                thread_id=approval.thread_id,
                user_id=approval.user_id,
                task_id=approval.task.id,
                tool=approval.task.tool,
                summary=approval.summary,
                plan=[_task_to_dict(t) for t in approval.plan],
                results=approval.results,
                completed=approval.completed,
                requested_at=approval.requested_at,
            )
        )
        await self.session.flush()

    async def get(self, thread_id: str, now: datetime | None = None) -> PendingApproval | None:
        now = now or datetime.now(timezone.utc)
        row = (
            await self.session.execute(select(Approval).where(Approval.thread_id == thread_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        if now - _aware(row.requested_at) > APPROVAL_TTL:
            await self.session.delete(row)
            await self.session.flush()
            return None
        return _row_to_approval(row)

    async def pop(self, thread_id: str, now: datetime | None = None) -> PendingApproval | None:
        approval = await self.get(thread_id, now)
        if approval is not None:
            await self.session.execute(
                delete(Approval).where(Approval.thread_id == thread_id)
            )
            await self.session.flush()
        return approval

    async def purge(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        result = await self.session.execute(
            delete(Approval).where(Approval.requested_at < now - APPROVAL_TTL)
        )
        await self.session.flush()
        return result.rowcount or 0


@dataclass
class PostgresSeenCache:
    """Déduplication des webhooks, durable.

    En mémoire, un redémarrage rouvre la porte aux doublons : les émetteurs
    retentent, et un message rejoué recrée le rendez-vous.
    """

    session: AsyncSession
    ttl: timedelta = timedelta(days=1)

    async def check_and_add(self, message_id: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        await self.session.execute(
            delete(SeenMessage).where(SeenMessage.created_at < now - self.ttl)
        )
        seen = (
            await self.session.execute(
                select(SeenMessage).where(SeenMessage.message_id == message_id)
            )
        ).scalar_one_or_none()
        if seen is not None:
            return False
        self.session.add(SeenMessage(message_id=message_id, created_at=now))
        await self.session.flush()
        return True


@dataclass
class PostgresCredentialStore:
    """Jetons OAuth chiffrés en base, mêmes garanties que le store mémoire."""

    session: AsyncSession
    key: bytes
    provider: str = "google"

    async def load(self, user_id: str):
        from ..integrations.google_oauth import GoogleCredentials
        from ..store import UnknownIntegration

        row = await self._row(user_id)
        if row is None:
            raise UnknownIntegration(
                f"Aucune intégration {self.provider} pour {user_id}. "
                "L'utilisateur doit d'abord autoriser l'accès."
            )
        aad = build_aad(user_id, self.provider)
        return GoogleCredentials(
            access_token=decrypt_token(row.access_token_enc, aad, self.key),
            refresh_token=decrypt_token(row.refresh_token_enc, aad, self.key),
            expires_at=row.expires_at,
        )

    async def save(self, user_id: str, credentials: Any) -> None:
        aad = build_aad(user_id, self.provider)
        access = encrypt_token(credentials.access_token, aad, self.key)
        refresh = encrypt_token(credentials.refresh_token, aad, self.key)

        row = await self._row(user_id)
        if row is None:
            self.session.add(
                Integration(
                    user_id=user_id,
                    provider=self.provider,
                    access_token_enc=access,
                    refresh_token_enc=refresh,
                    expires_at=credentials.expires_at,
                )
            )
        else:
            row.access_token_enc = access
            row.refresh_token_enc = refresh
            row.expires_at = credentials.expires_at
        await self.session.flush()

    async def exists(self, user_id: str) -> bool:
        return await self._row(user_id) is not None

    async def _row(self, user_id: str) -> Integration | None:
        return (
            await self.session.execute(
                select(Integration).where(
                    Integration.user_id == user_id, Integration.provider == self.provider
                )
            )
        ).scalar_one_or_none()


# --- conversions -----------------------------------------------------------


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "tool": task.tool,
        "args": task.args,
        "depends_on": list(task.depends_on),
        "priority": task.priority,
    }


def _dict_to_task(raw: dict) -> Task:
    return Task(
        raw["id"],
        raw["tool"],
        raw.get("args") or {},
        tuple(raw.get("depends_on") or ()),
        raw.get("priority", 0),
    )


def _row_to_approval(row: Approval) -> PendingApproval:
    plan = [_dict_to_task(t) for t in row.plan]
    task = next((t for t in plan if t.id == row.task_id), Task(row.task_id, row.tool))
    return PendingApproval(
        thread_id=row.thread_id,
        user_id=row.user_id,
        task=task,
        plan=plan,
        results=row.results or {},
        completed=row.completed or [],
        summary=row.summary,
        requested_at=_aware(row.requested_at),
    )
