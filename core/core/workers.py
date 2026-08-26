"""Worker ARQ — files à priorités et tâches périodiques.

Un rapport hebdomadaire de trois minutes ne doit jamais retarder un « quelle
heure est mon RDV ». D'où des files séparées, consommées par des pools distincts :

    P0  interactif   — requête utilisateur en cours de conversation
    P1  < 1 min      — suivi, envoi confirmé
    P2  < 15 min     — synchronisation, triage, indexation mémoire
    P3  best effort  — résumés longs, rapports

Un seul pool partagé suffirait fonctionnellement, mais une tâche lente y bloque
la file entière : c'est précisément ce qu'on veut éviter côté latence perçue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum

log = logging.getLogger("nova.worker")


class Priority(IntEnum):
    INTERACTIVE = 0
    FOLLOW_UP = 1
    BACKGROUND = 2
    BEST_EFFORT = 3

    @property
    def queue(self) -> str:
        return f"nova:p{int(self)}"


#: Verrou par ressource : deux créations concurrentes sur le même agenda
#: produisent des doublons que l'idempotence seule ne rattrape pas, puisque
#: les clés diffèrent.
def resource_lock(user_id: str, resource: str) -> str:
    return f"nova:lock:{user_id}:{resource}"


@dataclass
class JobContext:
    """Dépendances injectées dans les jobs au démarrage du worker."""

    pipeline: object
    registry: object
    approvals: object


async def handle_message_job(ctx: dict, user_id: str, payload: dict) -> str:
    """Job P0 : traite un message entrant de bout en bout."""
    from .pipeline import IncomingMessage

    deps: JobContext = ctx["deps"]
    message = IncomingMessage(
        id=payload["id"],
        user_id=user_id,
        thread_id=payload.get("thread_id") or f"{payload['channel']}:{payload['from_id']}",
        channel=payload["channel"],
        from_id=payload["from_id"],
        kind=payload.get("kind", "text"),
        text=payload.get("text"),
        media_url=payload.get("media_url"),
    )
    return await deps.pipeline.handle(message)


async def purge_approvals_job(ctx: dict) -> int:
    """Job P2 : retire les validations périmées. L'agent ne relance jamais."""
    deps: JobContext = ctx["deps"]
    removed = deps.approvals.purge()
    if removed:
        log.info("%d validation(s) expirée(s) purgée(s)", removed)
    return removed


async def sync_calendars_job(ctx: dict, user_id: str) -> int:
    """Job P2 : synchronisation incrémentale, avec resync complet sur 410."""
    from .providers.google_calendar import SyncTokenExpired

    deps: JobContext = ctx["deps"]
    provider = getattr(deps, "calendar_provider", None)
    if provider is None:
        return 0
    try:
        result = await provider.sync(sync_token=ctx.get("sync_token"))
    except SyncTokenExpired:
        log.info("syncToken expiré, resynchronisation complète")
        result = await provider.sync(sync_token=None)
    ctx["sync_token"] = result.next_sync_token
    return len(result.events)


async def startup(ctx: dict) -> None:
    log.info("worker démarré")


async def shutdown(ctx: dict) -> None:
    log.info("worker arrêté")


class WorkerSettings:
    """Configuration ARQ. Lancement : `arq core.workers.WorkerSettings`.

    `max_tries` et `retry_delay` valent pour les erreurs transitoires ; les
    erreurs métier ne sont pas rejouées — un plan invalide le restera.
    """

    functions = [handle_message_job, purge_approvals_job, sync_calendars_job]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = Priority.INTERACTIVE.queue
    max_jobs = 10
    job_timeout = 120
    max_tries = 3
    keep_result = 3600

    @staticmethod
    def cron_jobs():
        """Tâches périodiques. Importé paresseusement : arq est optionnel en test."""
        from arq import cron  # noqa: PLC0415

        return [
            cron(purge_approvals_job, minute={0, 15, 30, 45}),
            cron(sync_calendars_job, minute={5, 20, 35, 50}),
        ]


class BackgroundWorkerSettings(WorkerSettings):
    """Pool séparé pour P2/P3 : une tâche lente n'y bloque pas l'interactif."""

    queue_name = Priority.BACKGROUND.queue
    max_jobs = 4
    job_timeout = 600
