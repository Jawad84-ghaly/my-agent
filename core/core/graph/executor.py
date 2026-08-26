"""Exécution parallèle d'un plan, avec suspension sur le Confirmation Gate.

L'exécuteur avance couche par couche. Dès qu'une tâche exige une validation, il
s'arrête *avant* de l'exécuter et rend la main : le graphe est suspendu, l'état
sérialisable, et la reprise se fait au message suivant.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..gate import GateDecision, requires_approval
from ..planning import Task, topological_layers
from ..tools.registry import ToolRegistry, ToolResult


@dataclass
class PendingApproval:
    task: Task
    decision: GateDecision
    prepared: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionState:
    results: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    pending_approval: PendingApproval | None = None

    @property
    def suspended(self) -> bool:
        return self.pending_approval is not None


async def execute_plan(
    tasks: list[Task],
    registry: ToolRegistry,
    state: ExecutionState | None = None,
    approved_task_ids: frozenset[str] = frozenset(),
) -> ExecutionState:
    """Exécute un plan jusqu'au bout, ou jusqu'à la première validation requise.

    `approved_task_ids` porte les tâches déjà validées par l'utilisateur : à la
    reprise, elles ne repassent pas par le gate.
    """
    state = state or ExecutionState()
    # La reprise repart d'un état propre : sans cela, la suspension précédente
    # resterait collée à l'état et le plan semblerait bloqué même une fois validé.
    state.pending_approval = None

    for layer in topological_layers(tasks):
        runnable: list[Task] = []

        for task in layer:
            if task.id in state.completed:
                continue

            decision = requires_approval(task)
            if decision.requires_approval and task.id not in approved_task_ids:
                state.pending_approval = PendingApproval(
                    task=task,
                    decision=decision,
                    prepared=_prepared_context(task, state.results),
                )
                return state  # suspension : rien de cette couche ne part
            runnable.append(task)

        if not runnable:
            continue

        outcomes = await asyncio.gather(
            *(_run(task, registry, state.results) for task in runnable)
        )

        # strict= : si gather rendait un nombre de resultats different, mieux vaut
        # une erreur qu'un resultat silencieusement perdu.
        for task, result in zip(runnable, outcomes, strict=True):
            if result.ok:
                state.results[task.id] = result.data
                state.completed.append(task.id)
            else:
                # On n'invente pas de succès : la tâche est marquée en échec et
                # ses dépendantes ne s'exécuteront pas (référence non résolue).
                state.failures[task.id] = result.error or "échec inconnu"

    return state


async def _run(task: Task, registry: ToolRegistry, results: dict[str, Any]) -> ToolResult:
    try:
        args = task.resolve_args(results)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, tool=task.tool, error=f"{type(exc).__name__}: {exc}")
    return await registry.call(task.tool, args, task_id=task.id)


def _prepared_context(task: Task, results: dict[str, Any]) -> dict[str, Any]:
    """Contexte affiché dans le récapitulatif de validation.

    Les arguments sont résolus au mieux : si une référence manque encore, on
    montre ce qu'on a plutôt que d'échouer sur l'affichage.
    """
    try:
        return task.resolve_args(results)
    except Exception:  # noqa: BLE001
        return dict(task.args)


def summarize(state: ExecutionState) -> str:
    """État final rapporté à l'utilisateur : fait / en attente / échoué."""
    lines = []
    if state.completed:
        lines.append(f"✅ {len(state.completed)} action(s) effectuée(s)")
    if state.pending_approval:
        lines.append(f"⏸️ en attente : {state.pending_approval.decision.reason}")
    for task_id, error in state.failures.items():
        lines.append(f"⚠️ échec {task_id} : {error}")
    return "\n".join(lines) or "Rien à faire."
