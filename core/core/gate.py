"""Confirmation Gate — la règle qui sépare un jouet d'un outil utilisable.

Toute action sortante ou irréversible est préparée puis suspendue en attente d'un
« ok » explicite. Le reste s'exécute librement : on ne demande jamais la
permission de lire.

La politique est déclarative et fermée par défaut : un outil inconnu du registre
est traité comme irréversible. Ajouter un outil sortant sans y penser ne doit pas
ouvrir une brèche silencieuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .planning import Task

#: Outils explicitement sûrs : lecture, brouillons, objets internes.
FREE_TOOLS: frozenset[str] = frozenset(
    {
        "calendar.list_events",
        "calendar.find_free_slots",
        "calendar.detect_conflicts",
        "mail.search",
        "mail.read",
        "mail.triage",
        "mail.label",
        "mail.draft",
        "contacts.resolve",
        "contacts.get",
        "contacts.recent_interactions",
        "tasks.create",
        "tasks.list",
        "tasks.complete",
        "reminders.schedule",
        "notes.capture",
        "notes.summarize",
        "web.fetch_page",
        "web.search",
        "memory.remember",
        "memory.recall",
    }
)

#: Outils toujours sous validation, quels que soient leurs arguments.
GATED_TOOLS: frozenset[str] = frozenset(
    {
        "mail.send",
        "calendar.delete_event",
        "whatsapp.send_to_third_party",
        "notify.push_external",
    }
)

#: Délai au-delà duquel une validation en attente est abandonnée.
APPROVAL_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class GateDecision:
    requires_approval: bool
    reason: str = ""


def requires_approval(task: Task) -> GateDecision:
    """Décide si une tâche doit être suspendue avant exécution."""
    if task.tool in GATED_TOOLS:
        return GateDecision(True, f"{task.tool} est une action sortante irréversible")

    # Un événement devient sortant dès qu'il invite quelqu'un : la création est
    # libre, l'invitation ne l'est pas.
    if task.tool in ("calendar.create_event", "calendar.update_event"):
        attendees = task.args.get("attendees") or []
        if attendees:
            return GateDecision(True, f"invite {len(attendees)} participant(s) externe(s)")
        return GateDecision(False)

    if task.tool in FREE_TOOLS:
        return GateDecision(False)

    # Fermé par défaut.
    return GateDecision(True, f"{task.tool} n'est pas déclaré comme action libre")


def plan_needs_gate(tasks: list[Task]) -> list[tuple[Task, GateDecision]]:
    """Toutes les tâches d'un plan qui devront passer par une validation."""
    flagged = []
    for task in tasks:
        decision = requires_approval(task)
        if decision.requires_approval:
            flagged.append((task, decision))
    return flagged


def is_expired(requested_at: datetime, now: datetime | None = None) -> bool:
    """Une validation sans réponse expire — l'agent ne relance pas."""
    now = now or datetime.now(timezone.utc)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    return now - requested_at > APPROVAL_TTL


APPROVE_WORDS = {"ok", "oui", "yes", "go", "vas-y", "envoie", "valide", "d'accord", "👍", "✅"}
REJECT_WORDS = {"non", "no", "annule", "stop", "laisse tomber", "cancel"}


def classify_reply(text: str) -> str:
    """Classe une réponse à une demande de validation : approve | reject | edit.

    Volontairement conservateur : tout ce qui n'est pas un accord ou un refus net
    est traité comme une demande de modification, jamais comme un accord.
    """
    normalized = text.strip().lower().rstrip("!. ")
    if normalized in APPROVE_WORDS:
        return "approve"
    if normalized in REJECT_WORDS:
        return "reject"
    return "edit"


def format_approval_summary(task: Task, prepared: dict[str, Any]) -> str:
    """Récapitulatif compact envoyé sur le canal, format WhatsApp."""
    if task.tool == "mail.send":
        body = (prepared.get("body") or "").strip().splitlines()
        excerpt = "\n".join(body[:4])
        return (
            "📤 *Prêt à envoyer*\n"
            f"À : {prepared.get('to_display') or prepared.get('to')}\n"
            f"Objet : {prepared.get('subject')}\n"
            "---\n"
            f"{excerpt}\n"
            "---\n"
            "Réponds *ok* pour envoyer, ou dis-moi quoi changer."
        )
    if task.tool == "calendar.delete_event":
        return (
            "🗑️ *Suppression*\n"
            f"{prepared.get('title')} — {prepared.get('start')}\n"
            "Réponds *ok* pour supprimer."
        )
    return (
        f"⚠️ *Validation requise*\n{task.tool}\n"
        "Réponds *ok* pour confirmer, ou dis-moi quoi changer."
    )
