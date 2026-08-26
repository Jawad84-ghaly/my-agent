"""Registre d'outils : schéma strict, résultat structuré, idempotence.

Trois invariants, chacun correspondant à une panne observée en production sur ce
type d'agent :

1. Un outil retourne un objet structuré, jamais du texte libre — sinon le modèle
   re-parse sa propre sortie et hallucine des champs.
2. Tout outil mutatif reçoit une `idempotency_key` dérivée de ses arguments — un
   retry après timeout ne doit pas créer trois réunions identiques.
3. Un échec est rapporté comme un échec — l'agent ne doit jamais annoncer un
   succès qui n'a pas eu lieu.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

ToolFn = Callable[..., Awaitable[Any]]


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Any = None
    error: str | None = None
    duration_ms: int = 0

    def unwrap(self) -> Any:
        if not self.ok:
            raise ToolFailure(f"{self.tool}: {self.error}")
        return self.data


class ToolFailure(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    fn: ToolFn
    mutating: bool = False
    description: str = ""


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, name: str, *, mutating: bool = False, description: str = ""):
        def decorator(fn: ToolFn) -> ToolFn:
            if name in self.tools:
                raise ValueError(f"outil déjà enregistré: {name}")
            self.tools[name] = ToolSpec(name, fn, mutating, description or (fn.__doc__ or ""))
            return fn

        return decorator

    def get(self, name: str) -> ToolSpec:
        if name not in self.tools:
            raise KeyError(f"outil inconnu: {name}")
        return self.tools[name]

    async def call(self, name: str, args: dict[str, Any], *, task_id: str = "") -> ToolResult:
        spec = self.get(name)
        payload = dict(args)
        if spec.mutating:
            payload.setdefault("idempotency_key", idempotency_key(task_id, name, args))

        started = time.perf_counter()
        try:
            data = await spec.fn(**payload)
        except Exception as exc:  # noqa: BLE001 — on rapporte l'erreur réelle
            return ToolResult(
                ok=False,
                tool=name,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=_elapsed(started),
            )
        return ToolResult(ok=True, tool=name, data=data, duration_ms=_elapsed(started))


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def idempotency_key(task_id: str, tool: str, args: dict[str, Any]) -> str:
    """Clé stable : mêmes arguments logiques → même clé, quel que soit l'ordre."""
    canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(f"{task_id}|{tool}|{canonical}".encode()).hexdigest()
    return digest[:32]


registry = ToolRegistry()
