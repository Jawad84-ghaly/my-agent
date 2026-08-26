"""Implémentations Anthropic de Router, Planner et Responder.

Trois nœuds, deux modèles. Le routeur tourne sur Haiku 4.5 : l'essentiel du
trafic est trivial et n'a pas besoin du gros modèle. La planification et la
réponse tournent sur Opus 5, avec réflexion adaptative.

Ce découpage vient de la spécification du projet ; ce n'est pas une économie
décidée ici. Il tient parce que le routeur ne décide de rien d'irréversible : au
pire il escalade vers Opus, jamais l'inverse (voir ROUTER_INSTRUCTIONS, qui lui
demande explicitement de classer vers le haut en cas de doute).

Deux garde-fous côté code, indépendants du modèle :

- **Le plan est validé contre le registre d'outils.** Un nom d'outil halluciné
  est écarté avant l'exécution, pas découvert à l'appel.
- **Un refus du modèle est traité comme un refus**, jamais lu comme du contenu :
  `stop_reason == "refusal"` est vérifié avant de toucher à `content`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .planning import Task
from .prompts import (
    PLANNER_INSTRUCTIONS,
    RESPONDER_INSTRUCTIONS,
    ROUTER_INSTRUCTIONS,
    context_block,
    system_blocks,
)

log = logging.getLogger("nova.llm")

ROUTER_MODEL = "claude-haiku-4-5"
PLANNER_MODEL = "claude-opus-5"
RESPONDER_MODEL = "claude-opus-5"

#: Repli côté serveur si un classifieur de sûreté décline la requête.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """Le modèle a décliné la requête. À remonter tel quel, jamais à contourner."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(explanation or f"requête déclinée ({category})")
        self.category = category


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "calendar", "email", "contacts", "task",
                "note", "question", "smalltalk", "multi",
            ],
        },
        "complexity": {"type": "string", "enum": ["trivial", "standard", "complex"]},
        "requires_tools": {"type": "boolean"},
        "irreversible_action_likely": {"type": "boolean"},
    },
    "required": ["intent", "complexity", "requires_tools", "irreversible_action_likely"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string"},
                    "args": {"type": "object", "additionalProperties": True},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "tool", "args", "depends_on"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}


def _text_of(response: Any) -> str:
    """Extrait le texte, en refusant d'abord les réponses déclinées.

    Lire `content` sans vérifier `stop_reason` sur un refus donne une chaîne vide
    ou un message d'excuse, que le reste du pipeline interpréterait comme un
    résultat valide.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMRefusal(
            getattr(details, "category", None), getattr(details, "explanation", None)
        )
    for block in response.content:
        if getattr(block, "type", None) == "text" and block.text.strip():
            return block.text
    return ""


@dataclass
class AnthropicRouter:
    """Classification bon marché en amont, pour éviter de réveiller Opus."""

    client: Any
    model: str = ROUTER_MODEL

    async def route(self, text: str, context: dict) -> dict:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                system=ROUTER_INSTRUCTIONS,
                messages=[{"role": "user", "content": text}],
                output_config={"format": {"type": "json_schema", "schema": ROUTE_SCHEMA}},
            )
            return json.loads(_text_of(response))
        except LLMRefusal:
            raise
        except Exception as exc:  # noqa: BLE001
            # Un routeur en panne ne doit pas faire tomber la requête : on
            # escalade vers le planificateur, qui sait traiter le cas général.
            log.warning("routage indisponible (%s), escalade vers le planificateur", exc)
            return {
                "intent": "multi",
                "complexity": "standard",
                "requires_tools": True,
                "irreversible_action_likely": False,
            }


@dataclass
class AnthropicPlanner:
    """Construit le DAG de tâches, puis le valide contre les outils réels."""

    client: Any
    tool_names: frozenset[str]
    user_name: str = "l'utilisateur"
    timezone: str = "Europe/Paris"
    integrations: list[str] = field(default_factory=list)
    model: str = PLANNER_MODEL
    effort: str = "high"

    async def plan(self, text: str, context: dict) -> list[Task]:
        catalogue = "\n".join(f"- {name}" for name in sorted(self.tool_names))
        now_iso = context.get("now").isoformat() if context.get("now") else ""

        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system_blocks(self.user_name),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{context_block(context.get('channel', 'desktop'), now_iso, self.timezone, self.integrations)}\n"
                        f"# OUTILS DISPONIBLES\n{catalogue}\n\n"
                        f"{PLANNER_INSTRUCTIONS}\n\n"
                        f"# DEMANDE\n{text}"
                    ),
                }
            ],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": PLAN_SCHEMA},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

        payload = json.loads(_text_of(response) or '{"tasks": []}')
        return self._validate(payload.get("tasks", []))

    def _validate(self, raw_tasks: list[dict]) -> list[Task]:
        """Écarte les outils inconnus et les dépendances mortes.

        Un nom d'outil halluciné doit disparaître ici : plus loin, il deviendrait
        une erreur d'exécution au milieu d'un plan à moitié appliqué.
        """
        kept: list[Task] = []
        known_ids: set[str] = set()

        for raw in raw_tasks:
            tool = raw.get("tool", "")
            if tool not in self.tool_names:
                log.warning("outil inconnu écarté du plan : %r", tool)
                continue
            task_id = raw.get("id") or f"T{len(kept) + 1}"
            depends = tuple(d for d in raw.get("depends_on", []) if d in known_ids)
            if len(depends) != len(raw.get("depends_on", [])):
                log.warning("%s : dépendance vers une tâche écartée, ignorée", task_id)
            kept.append(Task(task_id, tool, raw.get("args") or {}, depends))
            known_ids.add(task_id)

        return kept


@dataclass
class AnthropicResponder:
    """Rédige le compte rendu final, adapté au canal."""

    client: Any
    user_name: str = "l'utilisateur"
    model: str = RESPONDER_MODEL
    effort: str = "medium"

    async def summarize(self, text: str, state: Any, context: dict) -> str:
        report = _state_report(state)

        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=1500,
            system=system_blocks(self.user_name),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"# CANAL\n{context.get('channel', 'desktop')}\n\n"
                        f"{RESPONDER_INSTRUCTIONS}\n\n"
                        f"# DEMANDE INITIALE\n{text}\n\n"
                        f"# RÉSULTAT DE L'EXÉCUTION\n{report}"
                    ),
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        return _text_of(response).strip()


def _state_report(state: Any) -> str:
    """Sérialise l'état d'exécution pour le modèle, échecs compris.

    Les échecs sont transmis explicitement : sans eux, le modèle rédigerait un
    compte rendu de succès pour des actions qui n'ont pas eu lieu.
    """
    completed = getattr(state, "completed", []) or []
    results = getattr(state, "results", {}) or {}
    failures = getattr(state, "failures", {}) or {}

    if not completed and not failures:
        return "Aucune action exécutée."

    lines = []
    for task_id in completed:
        lines.append(f"- {task_id} : OK — {json.dumps(results.get(task_id), default=str, ensure_ascii=False)}")
    for task_id, error in failures.items():
        lines.append(f"- {task_id} : ÉCHEC — {error}")
    return "\n".join(lines)


def build_nodes(
    client: Any,
    tool_names: frozenset[str],
    user_name: str = "l'utilisateur",
    timezone: str = "Europe/Paris",
    integrations: list[str] | None = None,
) -> tuple[AnthropicRouter, AnthropicPlanner, AnthropicResponder]:
    """Construit les trois nœuds à partir d'un client Anthropic asynchrone.

        client = anthropic.AsyncAnthropic()
        router, planner, responder = build_nodes(client, frozenset(registry.tools))
        pipeline = Pipeline(registry, router, planner, responder, sender)
    """
    return (
        AnthropicRouter(client),
        AnthropicPlanner(client, tool_names, user_name, timezone, list(integrations or [])),
        AnthropicResponder(client, user_name),
    )
