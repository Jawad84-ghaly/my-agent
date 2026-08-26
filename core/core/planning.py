"""Décomposition d'une requête en DAG de tâches atomiques.

L'agent produit un plan : une liste de tâches avec leurs dépendances. L'exécuteur
regroupe ensuite ce plan en *couches* — chaque couche ne contient que des tâches
mutuellement indépendantes, donc exécutables en parallèle.

    T1: contacts.resolve("Marc")          ─┐
    T2: calendar.detect_conflicts(...)    ─┴─ couche 0 (parallèle)
    T3: calendar.create_event(...)          ─ couche 1 (dépend de T1, T2)
    T4: mail.draft(...)                     ─ couche 2 (dépend de T1, T3)
    T5: mail.send(...)                      ─ couche 3 (gate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PlanError(ValueError):
    """Plan invalide : dépendance inconnue ou cycle."""


@dataclass(frozen=True)
class Task:
    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    priority: int = 0

    def resolve_args(self, results: dict[str, Any]) -> dict[str, Any]:
        """Remplace les références `{{T1.email}}` par la valeur produite par T1.

        Une référence non résolue est une erreur de plan, pas une valeur vide :
        laisser passer un `None` ici enverrait un email à personne.
        """
        return {k: _substitute(v, results, self.id) for k, v in self.args.items()}


def _substitute(value: Any, results: dict[str, Any], task_id: str) -> Any:
    if isinstance(value, list):
        return [_substitute(v, results, task_id) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, results, task_id) for k, v in value.items()}
    if not (isinstance(value, str) and value.startswith("{{") and value.endswith("}}")):
        return value

    ref = value[2:-2].strip()
    head, _, path = ref.partition(".")
    if head not in results:
        raise PlanError(f"{task_id}: référence vers une tâche inconnue ou non exécutée: {ref}")

    current = results[head]
    for part in filter(None, path.split(".")):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            raise PlanError(f"{task_id}: chemin introuvable dans le résultat: {ref}")
    return current


def topological_layers(tasks: list[Task]) -> list[list[Task]]:
    """Regroupe les tâches en couches parallélisables.

    Lève PlanError sur une dépendance inconnue ou un cycle — mieux vaut refuser
    un plan que d'en exécuter la moitié.
    """
    by_id: dict[str, Task] = {}
    for task in tasks:
        if task.id in by_id:
            raise PlanError(f"identifiant de tâche dupliqué: {task.id}")
        by_id[task.id] = task

    for task in tasks:
        for dep in task.depends_on:
            if dep not in by_id:
                raise PlanError(f"{task.id}: dépendance inconnue {dep}")

    pending = dict(by_id)
    done: set[str] = set()
    layers: list[list[Task]] = []

    while pending:
        ready = [t for t in pending.values() if set(t.depends_on) <= done]
        if not ready:
            raise PlanError(f"cycle de dépendances entre: {sorted(pending)}")
        ready.sort(key=lambda t: (-t.priority, t.id))
        layers.append(ready)
        for task in ready:
            del pending[task.id]
            done.add(task.id)

    return layers
