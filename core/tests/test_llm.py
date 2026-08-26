"""Tests des nœuds LLM — aucun appel réseau, aucune clé API requise.

Le client Anthropic est remplacé par un double qui rejoue des réponses scriptées
et enregistre les requêtes : on vérifie la forme des appels (modèle, cache,
schéma) et le traitement des réponses, y compris les cas dégradés.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from core.graph.executor import ExecutionState
from core.llm import (
    AnthropicPlanner,
    AnthropicResponder,
    AnthropicRouter,
    LLMRefusal,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
TOOLS = frozenset({"contacts.resolve", "calendar.create_event", "mail.draft", "mail.send"})


def run(coro):
    return asyncio.run(coro)


# --- double de client ------------------------------------------------------

@dataclass
class FakeBlock:
    text: str
    type: str = "text"


@dataclass
class FakeDetails:
    category: str | None = None
    explanation: str | None = None


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
    """Expose `.messages` et `.beta.messages` sur le même enregistreur."""

    messages: FakeMessages

    @property
    def beta(self):
        return self

    @property
    def calls(self):
        return self.messages.calls


def client_returning(*payloads) -> FakeClient:
    responses = [
        p if isinstance(p, Exception)
        else FakeResponse([FakeBlock(p if isinstance(p, str) else json.dumps(p))])
        for p in payloads
    ]
    return FakeClient(FakeMessages(responses))


# --- routeur ---------------------------------------------------------------

def test_router_parses_the_classification():
    client = client_returning({
        "intent": "calendar", "complexity": "standard",
        "requires_tools": True, "irreversible_action_likely": False,
    })
    decision = run(AnthropicRouter(client).route("RDV demain", {}))
    assert decision["intent"] == "calendar"
    assert decision["requires_tools"] is True


def test_router_uses_the_cheap_model():
    """Le routage porte l'essentiel du trafic : il ne doit pas réveiller Opus."""
    client = client_returning({"intent": "smalltalk", "complexity": "trivial",
                               "requires_tools": False, "irreversible_action_likely": False})
    run(AnthropicRouter(client).route("bonjour", {}))
    assert client.calls[0]["model"] == "claude-haiku-4-5"


def test_router_constrains_the_output_schema():
    client = client_returning({"intent": "task", "complexity": "trivial",
                               "requires_tools": False, "irreversible_action_likely": False})
    run(AnthropicRouter(client).route("note ça", {}))
    fmt = client.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False


def test_router_failure_escalates_instead_of_breaking():
    """Un routeur en panne doit dégrader vers le planificateur, pas tout casser."""
    client = client_returning(RuntimeError("503"))
    decision = run(AnthropicRouter(client).route("RDV demain", {}))
    assert decision["requires_tools"] is True
    assert decision["complexity"] == "standard"


def test_router_refusal_is_not_swallowed():
    client = FakeClient(FakeMessages([
        FakeResponse([], stop_reason="refusal", stop_details=FakeDetails("cyber", "décliné"))
    ]))
    with pytest.raises(LLMRefusal):
        run(AnthropicRouter(client).route("quelque chose", {}))


# --- planificateur ---------------------------------------------------------

def plan_payload(*tasks):
    return {"tasks": list(tasks)}


def test_planner_builds_tasks_with_dependencies():
    client = client_returning(plan_payload(
        {"id": "T1", "tool": "contacts.resolve", "args": {"query": "Marc"}, "depends_on": []},
        {"id": "T2", "tool": "mail.draft", "args": {"to": "{{T1.email}}"}, "depends_on": ["T1"]},
    ))
    plan = run(AnthropicPlanner(client, TOOLS).plan("écris à Marc", {"now": NOW}))
    assert [t.id for t in plan] == ["T1", "T2"]
    assert plan[1].depends_on == ("T1",)
    assert plan[1].args["to"] == "{{T1.email}}"


def test_planner_drops_hallucinated_tools():
    """Un outil inventé doit disparaître ici, pas exploser en cours d'exécution."""
    client = client_returning(plan_payload(
        {"id": "T1", "tool": "contacts.resolve", "args": {}, "depends_on": []},
        {"id": "T2", "tool": "telepathy.send", "args": {}, "depends_on": []},
    ))
    plan = run(AnthropicPlanner(client, TOOLS).plan("fais un truc", {"now": NOW}))
    assert [t.tool for t in plan] == ["contacts.resolve"]


def test_planner_drops_dependencies_on_removed_tasks():
    """Une dépendance orpheline rendrait le plan inexécutable."""
    client = client_returning(plan_payload(
        {"id": "T1", "tool": "telepathy.send", "args": {}, "depends_on": []},
        {"id": "T2", "tool": "mail.draft", "args": {}, "depends_on": ["T1"]},
    ))
    plan = run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))
    assert len(plan) == 1
    assert plan[0].depends_on == ()


def test_planner_returns_empty_plan_when_model_declines_to_plan():
    client = client_returning(plan_payload())
    assert run(AnthropicPlanner(client, TOOLS).plan("???", {"now": NOW})) == []


def test_planner_lists_only_real_tools_in_the_prompt():
    client = client_returning(plan_payload())
    run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))
    prompt = client.calls[0]["messages"][0]["content"]
    assert "contacts.resolve" in prompt
    assert "telepathy.send" not in prompt


def test_planner_caches_the_stable_system_prompt():
    """Le prompt système est long et invariant : c'est lui qu'on met en cache."""
    client = client_returning(plan_payload())
    run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))
    system = client.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_volatile_context_stays_out_of_the_cached_block():
    """Une date dans le préfixe caché invaliderait le cache à chaque tour."""
    client = client_returning(plan_payload())
    run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW, "channel": "whatsapp"}))
    cached = client.calls[0]["system"][0]["text"]
    assert "2026-08-26" not in cached
    assert "whatsapp" not in cached
    assert "2026-08-26" in client.calls[0]["messages"][0]["content"]


def test_planner_uses_adaptive_thinking_and_effort():
    client = client_returning(plan_payload())
    run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "high"


def test_planner_enables_refusal_fallbacks():
    client = client_returning(plan_payload())
    run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))
    assert "server-side-fallback-2026-07-01" in client.calls[0]["betas"]
    assert client.calls[0]["fallbacks"] == "default"


def test_planner_refusal_propagates():
    client = FakeClient(FakeMessages([
        FakeResponse([], stop_reason="refusal", stop_details=FakeDetails("bio", None))
    ]))
    with pytest.raises(LLMRefusal):
        run(AnthropicPlanner(client, TOOLS).plan("x", {"now": NOW}))


# --- rédacteur -------------------------------------------------------------

def test_responder_returns_the_text():
    client = client_returning("✅ RDV créé mardi 10 h.")
    state = ExecutionState(results={"T1": {"id": "evt"}}, completed=["T1"])
    out = run(AnthropicResponder(client).summarize("RDV mardi", state, {"channel": "whatsapp"}))
    assert out == "✅ RDV créé mardi 10 h."


def test_failures_are_given_to_the_model():
    """Sans les échecs dans le contexte, le modèle annonce un succès inexistant."""
    client = client_returning("⚠️ L'envoi a échoué.")
    state = ExecutionState(failures={"T3": "SMTP timeout"})
    run(AnthropicResponder(client).summarize("envoie", state, {}))
    prompt = client.calls[0]["messages"][0]["content"]
    assert "ÉCHEC" in prompt
    assert "SMTP timeout" in prompt


def test_channel_is_passed_so_length_can_adapt():
    client = client_returning("ok")
    run(AnthropicResponder(client).summarize("x", ExecutionState(), {"channel": "whatsapp"}))
    assert "whatsapp" in client.calls[0]["messages"][0]["content"]


def test_empty_state_is_described_as_such():
    client = client_returning("Rien à faire.")
    run(AnthropicResponder(client).summarize("x", ExecutionState(), {}))
    assert "Aucune action exécutée" in client.calls[0]["messages"][0]["content"]
