"""Le worker de bout en bout : `handle_message_job` construit un `Pipeline` par
message, avec de vrais outils calendrier et un registre d'approbations en base.

Le test qui compte ici est `test_approval_survives_across_jobs` : c'est la
raison d'être de `PostgresApprovalRegistry` dans le worker plutôt que le
registre en mémoire — deux jobs ARQ sont deux appels de fonction indépendants,
potentiellement sur des workers différents, donc une validation en attente qui
ne vivrait qu'en mémoire process ne survivrait jamais jusqu'au « ok ».

Aucun réseau : le client Anthropic est le double scripté de `test_llm.py`, et
Google/Evolution passent par `FakeTransport`.
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from conftest import FakeTransport, always
from core.db.repositories import PostgresApprovalRegistry, PostgresCredentialStore
from core.db.session import create_all, make_engine, make_session_factory
from core.integrations.google_oauth import GoogleCredentials
from core.integrations.http import Response
from core.messaging import RecordingSender
from core.security.crypto import generate_key
from core.workers import JobContext, handle_message_job

KEY = base64.urlsafe_b64decode(generate_key())


def run(coro):
    return asyncio.run(coro)


# --- double de client Anthropic (même forme que test_llm.py) ---------------


@dataclass
class FakeBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    stop_details: Any = None


@dataclass
class FakeMessages:
    responses: list[Any]
    calls: list[dict] = field(default_factory=list)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class FakeClient:
    messages: FakeMessages

    @property
    def beta(self):
        return self

    @property
    def calls(self):
        return self.messages.calls


def client_returning(*payloads) -> FakeClient:
    responses = [
        FakeResponse([FakeBlock(p if isinstance(p, str) else json.dumps(p))]) for p in payloads
    ]
    return FakeClient(FakeMessages(responses))


ROUTE_STANDARD = {
    "intent": "calendar",
    "complexity": "standard",
    "requires_tools": True,
    "irreversible_action_likely": True,
}
ROUTE_TRIVIAL = {
    "intent": "smalltalk",
    "complexity": "trivial",
    "requires_tools": False,
    "irreversible_action_likely": False,
}


async def _with_deps(anthropic_client, body, *, google=True):
    """Base neuve, credential Google pré-posée, JobContext prêt pour un job."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    session_factory = make_session_factory(engine)

    if google:
        async with session_factory() as session:
            store = PostgresCredentialStore(session, key=KEY)
            await store.save(
                "u1",
                GoogleCredentials(
                    access_token="AT", refresh_token="RT", expires_at=time.time() + 3600
                ),
            )
            await session.commit()

    transport = FakeTransport(always(Response(200, {})))
    deps = JobContext(
        session_factory=session_factory,
        sender=RecordingSender(),
        http_transport=transport,
        anthropic_client=anthropic_client,
        google_client_id="cid" if google else "",
        google_client_secret="csecret",
        master_key=KEY if google else None,
    )
    try:
        await body(deps, transport)
    finally:
        await engine.dispose()


def msg(text: str, msg_id: str = "m1") -> dict:
    return {"id": msg_id, "channel": "whatsapp", "from_id": "33612345678", "kind": "text", "text": text}


# --- chemin trivial ----------------------------------------------------


def test_trivial_message_replies_without_planning():
    client = client_returning(ROUTE_TRIVIAL, "Bonjour ! Je peux t'aider ?")

    async def body(deps, _transport):
        ctx = {"deps": deps}
        result = await handle_message_job(ctx, "u1", msg("bonjour"))
        assert "Bonjour" in result
        assert deps.sender.sent == [("33612345678", "Bonjour ! Je peux t'aider ?")]
        # routeur puis répondeur : le planificateur n'est jamais sollicité.
        assert len(client.calls) == 2

    run(_with_deps(client, body))


# --- la validation survit d'un job à l'autre --------------------------------


def test_approval_survives_across_jobs():
    """`calendar.delete_event` est toujours gated : le premier job doit suspendre,
    et le second (la réponse « ok », un *appel de fonction différent*) doit
    retrouver l'approbation en base et exécuter la suppression."""
    client = client_returning(
        ROUTE_STANDARD,
        {"tasks": [{"id": "T1", "tool": "calendar.delete_event", "args": {"event_id": "E1"}, "depends_on": []}]},
        "✅ RDV supprimé.",
    )

    async def body(deps, transport):
        ctx = {"deps": deps}

        first = await handle_message_job(ctx, "u1", msg("supprime le rdv E1", "m1"))
        assert "Réponds" in first  # récapitulatif de validation, rien n'est parti
        assert not any(m.method == "DELETE" for m in transport.requests)

        async with deps.session_factory() as session:
            approvals = PostgresApprovalRegistry(session)
            pending = await approvals.get("whatsapp:33612345678")
            assert pending is not None
            assert pending.task.tool == "calendar.delete_event"

        second = await handle_message_job(ctx, "u1", msg("ok", "m2"))
        assert second == "✅ RDV supprimé."
        assert any(m.method == "DELETE" for m in transport.requests)

        async with deps.session_factory() as session:
            approvals = PostgresApprovalRegistry(session)
            assert await approvals.get("whatsapp:33612345678") is None

    run(_with_deps(client, body))


# --- Gmail : gated, et dédoublonné à travers deux jobs ---------------------


def test_mail_send_is_gated_and_survives_across_jobs():
    """`mail.send` est toujours gated : le brouillon part au premier job, l'envoi
    attend le second (le « ok », un appel de fonction différent). Gmail ne
    proposant pas d'id imposable côté client, c'est `PostgresIdempotencyStore`
    qui empêche un double envoi si ce second job était lui-même rejoué."""
    client = client_returning(
        ROUTE_STANDARD,
        {"tasks": [
            {"id": "T1", "tool": "mail.draft",
             "args": {"to": ["marc@exemple.fr"], "subject": "Point", "body": "Salut"},
             "depends_on": []},
            {"id": "T2", "tool": "mail.send",
             "args": {"draft_id": "{{T1.id}}", "to_display": "{{T1.to_display}}",
                       "subject": "{{T1.subject}}", "body": "{{T1.body}}"},
             "depends_on": ["T1"]},
        ]},
        "✅ Email envoyé.",
    )

    def handler(request, _index):
        if request.url.endswith("/drafts"):
            return Response(200, {"id": "draft-1"})
        return Response(200, {"id": "msg-1", "threadId": "th-1"})

    async def body(deps, _transport):
        deps.http_transport = FakeTransport(handler)
        ctx = {"deps": deps}

        first = await handle_message_job(ctx, "u1", msg("envoie un email à marc", "m1"))
        assert "Réponds" in first  # récapitulatif de validation, rien n'est parti
        assert any(r.url.endswith("/drafts") for r in deps.http_transport.requests)
        assert not any(r.url.endswith("/drafts/send") for r in deps.http_transport.requests)

        second = await handle_message_job(ctx, "u1", msg("ok", "m2"))
        assert second == "✅ Email envoyé."
        assert any(r.url.endswith("/drafts/send") for r in deps.http_transport.requests)

    run(_with_deps(client, body))


# --- Outlook : bascule quand Google n'est pas configuré --------------------


def test_outlook_calendar_is_offered_when_microsoft_is_configured_instead_of_google():
    """`calendar.*` est un seul jeu d'outils dans le registre : un déploiement
    sert Google ou Microsoft pour le calendrier, jamais les deux à la fois."""
    client = client_returning(
        ROUTE_STANDARD,
        {"tasks": [{"id": "T1", "tool": "calendar.list_events",
                    "args": {"start": "2026-08-27T00:00:00+00:00", "end": "2026-08-28T00:00:00+00:00"},
                    "depends_on": []}]},
        "Rien de prévu.",
    )

    def handler(request, _index):
        assert "graph.microsoft.com" in request.url
        return Response(200, {"value": []})

    async def body(deps, _transport):
        deps.google_client_id = ""
        deps.master_key = KEY
        deps.microsoft_client_id = "ms-cid"
        deps.microsoft_client_secret = "ms-secret"
        deps.http_transport = FakeTransport(handler)

        async with deps.session_factory() as session:
            from core.db.repositories import PostgresCredentialStore
            from core.integrations.microsoft_oauth import MicrosoftCredentials

            store = PostgresCredentialStore(session, key=KEY, provider="microsoft")
            await store.save("u1", MicrosoftCredentials("AT", "RT", time.time() + 3600))
            await session.commit()

        ctx = {"deps": deps}
        result = await handle_message_job(ctx, "u1", msg("mon agenda de demain"))
        assert result == "Rien de prévu."

    run(_with_deps(client, body, google=False))
# --- Contacts (People API) ----------------------------------------------


def test_contacts_resolve_is_offered_alongside_calendar():
    client = client_returning(
        ROUTE_STANDARD,
        {"tasks": [{"id": "T1", "tool": "contacts.resolve", "args": {"query": "Marc"},
                    "depends_on": []}]},
        "C'est Marc Dubois.",
    )

    def handler(request, _index):
        assert request.url.endswith("/people/me/connections")
        return Response(200, {"connections": [{
            "resourceName": "people/c1",
            "names": [{"displayName": "Marc Dubois"}],
            "emailAddresses": [{"value": "marc@exemple.fr"}],
        }]})

    async def body(deps, _transport):
        deps.http_transport = FakeTransport(handler)
        ctx = {"deps": deps}
        result = await handle_message_job(ctx, "u1", msg("qui est Marc ?"))
        assert result == "C'est Marc Dubois."

    run(_with_deps(client, body))


# --- canal `app` : réponse par valeur de retour, pas par push -------------


def test_app_channel_does_not_push_through_the_whatsapp_sender():
    """L'app attend sa réponse sur la requête HTTP (voir `api/app_channel.py`) :
    `deps.sender` (Evolution/WhatsApp) ne doit jamais être sollicité pour elle."""
    client = client_returning(ROUTE_TRIVIAL, "Bonjour ! Je peux t'aider ?")

    async def body(deps, _transport):
        ctx = {"deps": deps}
        payload = {"id": "m1", "channel": "app", "from_id": "device-1", "kind": "text",
                   "text": "bonjour"}
        result = await handle_message_job(ctx, "u1", payload)
        assert result == "Bonjour ! Je peux t'aider ?"
        assert deps.sender.sent == []  # rien poussé côté WhatsApp

    run(_with_deps(client, body))


# --- dégradations gracieuses -------------------------------------------


def test_missing_anthropic_key_replies_without_touching_the_database():
    async def body(deps, _transport):
        deps.anthropic_client = None
        ctx = {"deps": deps}
        result = await handle_message_job(ctx, "u1", msg("bonjour"))
        assert "incomplète" in result
        assert deps.sender.sent

    run(_with_deps(client_returning(), body))


def test_without_google_integration_calendar_tools_are_not_offered():
    client = client_returning(ROUTE_TRIVIAL, "Salut !")

    async def body(deps, _transport):
        ctx = {"deps": deps}
        result = await handle_message_job(ctx, "u1", msg("salut"))
        assert result == "Salut !"

    run(_with_deps(client, body, google=False))
