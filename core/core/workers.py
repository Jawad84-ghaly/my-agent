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
import os
from dataclasses import dataclass
from enum import IntEnum

try:
    from arq.connections import RedisSettings
except ImportError:  # arq optionnel en test/dev sans les extras "worker"
    RedisSettings = None

log = logging.getLogger("nova.worker")


def _redis_settings():
    """None laisse arq sur son défaut (localhost) — dev sans docker-compose."""
    url = os.environ.get("REDIS_URL")
    if RedisSettings is None or not url:
        return None
    return RedisSettings.from_dsn(url)


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
    """Ressources partagées par tous les jobs, construites une fois au démarrage.

    Pas de `Pipeline` tout fait ici : ses outils calendrier dépendent des
    identifiants Google d'un utilisateur précis (`access_token_provider` clôt
    sur son `user_id`), donc `handle_message_job` en construit un neuf à
    chaque message plutôt que d'en partager un entre utilisateurs.
    """

    session_factory: object
    sender: object
    http_transport: object
    anthropic_client: object | None
    google_client_id: str
    google_client_secret: str
    master_key: bytes | None
    engine: object | None = None
    calendar_provider: object | None = None  # utilisé par sync_calendars_job
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""


async def handle_message_job(ctx: dict, user_id: str, payload: dict) -> str:
    """Job P0 : traite un message entrant de bout en bout.

    Construit un `Pipeline` propre à ce message : les outils calendrier sont
    fermés sur les identifiants Google de `user_id`, donc un registre partagé
    entre jobs ferait fuiter l'agenda d'un utilisateur vers un autre.
    """
    from .db.repositories import (
        PostgresApprovalRegistry,
        PostgresCredentialStore,
        PostgresIdempotencyStore,
    )
    from .db.session import session_scope
    from .integrations.google_oauth import ensure_fresh
    from .integrations.microsoft_oauth import ensure_fresh as ensure_fresh_microsoft
    from .llm import build_nodes
    from .messaging import ChannelFormatter
    from .pipeline import IncomingMessage, Pipeline
    from .providers.gmail import GmailProvider
    from .providers.google_calendar import GoogleCalendar
    from .providers.outlook_calendar import OutlookCalendar
    from .tools.calendar_tools import register_calendar_tools
    from .tools.mail_tools import register_mail_tools
    from .providers.google_people import GooglePeopleProvider
    from .tools.calendar_tools import register_calendar_tools
    from .tools.contacts_tools import register_contacts_tools
    from .tools.registry import ToolRegistry

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

    if deps.anthropic_client is None:
        log.error("ANTHROPIC_API_KEY absent : message %s abandonné", message.id)
        body = "⚠️ Configuration incomplète côté serveur, contacte l'administrateur."
        for chunk in ChannelFormatter(message.channel).render(body):
            await deps.sender.send_text(message.from_id, chunk)
        return body

    async with session_scope(deps.session_factory) as session:
        tools = ToolRegistry()
        integrations: list[str] = []

        if deps.google_client_id and deps.master_key:
            store = PostgresCredentialStore(session, key=deps.master_key)

            async def _access_token() -> str:
                creds = await ensure_fresh(
                    deps.http_transport,
                    store,
                    user_id,
                    deps.google_client_id,
                    deps.google_client_secret,
                )
                return creds.access_token

            provider = GoogleCalendar(deps.http_transport, _access_token)
            register_calendar_tools(tools, provider)
            integrations.append("google_calendar")

            # Même jeton Google, mêmes scopes (gmail.modify/gmail.send déjà
            # demandés par google_oauth.py) : pas de second CredentialStore.
            gmail = GmailProvider(deps.http_transport, _access_token, PostgresIdempotencyStore(session))
            register_mail_tools(tools, gmail)
            integrations.append("gmail")
        elif deps.microsoft_client_id and deps.master_key:
            # `calendar.*` est un seul jeu d'outils dans le registre : un
            # déploiement sert soit Google, soit Microsoft pour le calendrier,
            # jamais les deux à la fois pour un même utilisateur.
            store = PostgresCredentialStore(session, key=deps.master_key, provider="microsoft")

            async def _access_token_ms() -> str:
                creds = await ensure_fresh_microsoft(
                    deps.http_transport,
                    store,
                    user_id,
                    deps.microsoft_client_id,
                    deps.microsoft_client_secret,
                )
                return creds.access_token

            provider = OutlookCalendar(
                deps.http_transport, _access_token_ms, PostgresIdempotencyStore(session)
            )
            register_calendar_tools(tools, provider)
            integrations.append("outlook_calendar")
            # Même jeton Google, mêmes scopes (contacts.readonly déjà demandé
            # par google_oauth.py) : pas de second CredentialStore.
            people = GooglePeopleProvider(deps.http_transport, _access_token)
            register_contacts_tools(tools, people)
            integrations.append("contacts")

        router, planner, responder = build_nodes(
            deps.anthropic_client, frozenset(tools.tools), integrations=integrations
        )
        pipeline = Pipeline(
            tools=tools,
            router=router,
            planner=planner,
            responder=responder,
            sender=deps.sender,
            approvals=PostgresApprovalRegistry(session),
        )
        return await pipeline.handle(message)


async def purge_approvals_job(ctx: dict) -> int:
    """Job P2 : retire les validations périmées. L'agent ne relance jamais."""
    deps: JobContext = ctx["deps"]
    removed = await deps.approvals.purge()
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
    """Construit les ressources longue durée du processus, une fois pour tous les jobs."""
    from .db.session import database_url, make_engine, make_session_factory
    from .integrations.http import HttpxTransport
    from .messaging import EvolutionSender

    engine = make_engine(database_url())
    http_transport = HttpxTransport()

    anthropic_client = None
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        import anthropic  # noqa: PLC0415 — extra "agent" optionnel

        anthropic_client = anthropic.AsyncAnthropic(api_key=anthropic_key)
    else:
        log.warning("ANTHROPIC_API_KEY absent : les messages seront refusés poliment")

    master_key = None
    try:
        from .security.crypto import load_master_key

        master_key = load_master_key()
    except Exception as exc:  # noqa: BLE001 — clé absente : dégrade, ne bloque pas WhatsApp
        log.warning("NOVA_MASTER_KEY absent ou invalide (%s) : pas d'outils Google", exc)

    ctx["deps"] = JobContext(
        session_factory=make_session_factory(engine),
        sender=EvolutionSender(
            transport=http_transport,
            base_url=os.environ.get("NOVA_EVOLUTION_URL", ""),
            instance=os.environ.get("NOVA_EVOLUTION_INSTANCE", ""),
            api_key=os.environ.get("NOVA_EVOLUTION_API_KEY", ""),
        ),
        http_transport=http_transport,
        anthropic_client=anthropic_client,
        google_client_id=os.environ.get("NOVA_GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("NOVA_GOOGLE_CLIENT_SECRET", ""),
        master_key=master_key,
        engine=engine,
        microsoft_client_id=os.environ.get("NOVA_MICROSOFT_CLIENT_ID", ""),
        microsoft_client_secret=os.environ.get("NOVA_MICROSOFT_CLIENT_SECRET", ""),
    )
    log.info("worker démarré")


async def shutdown(ctx: dict) -> None:
    deps: JobContext | None = ctx.get("deps")
    if deps is not None:
        await deps.http_transport.aclose()
        if deps.engine is not None:
            await deps.engine.dispose()
    log.info("worker arrêté")


class WorkerSettings:
    """Configuration ARQ. Lancement : `arq core.workers.WorkerSettings`.

    `max_tries` et `retry_delay` valent pour les erreurs transitoires ; les
    erreurs métier ne sont pas rejouées — un plan invalide le restera.
    """

    functions = [handle_message_job, purge_approvals_job, sync_calendars_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
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
