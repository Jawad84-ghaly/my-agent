"""Outils `mail.draft`/`mail.send` — deuxième intégration avec un vrai backend.

Le prompt du planificateur impose déjà l'ordre : un envoi suppose un brouillon
(`mail.draft` puis `mail.send`, jamais un envoi direct). `mail.send` est gated
(`core/gate.py`) — son résumé de validation attend `to`/`to_display`/`subject`/
`body` dans les arguments résolus de la tâche, donc `mail.send` les reprend
explicitement plutôt que de ne référencer que `draft_id`.

`contacts.resolve` n'a pas d'implémentation (pas de People API) : un plan qui
chaîne `contacts.resolve` avant `mail.draft` verra cette première tâche
écartée comme outil inconnu — l'utilisateur doit donc encore fournir des
adresses email directement.
"""

from __future__ import annotations

from .registry import ToolRegistry
from ..providers.mail import Draft, MailProvider, SentMessage


def _draft_dict(draft: Draft) -> dict:
    return {
        "id": draft.id,
        "to": draft.to,
        "to_display": ", ".join(draft.to),
        "cc": draft.cc,
        "subject": draft.subject,
        "body": draft.body,
    }


def _sent_dict(message: SentMessage) -> dict:
    return {"id": message.id, "thread_id": message.thread_id}


def register_mail_tools(registry: ToolRegistry, provider: MailProvider) -> None:
    """Enregistre les outils `mail.*` liés à ce provider — donc à cet utilisateur.

    Comme pour `register_calendar_tools`, un `ToolRegistry` neuf par job : le
    provider est fermé sur les identifiants Google d'un seul utilisateur.
    """

    @registry.register(
        "mail.draft", mutating=True, description="Prépare un brouillon. N'envoie rien."
    )
    async def _draft(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        idempotency_key: str = "",
    ) -> dict:
        draft = await provider.create_draft(to, subject, body, idempotency_key, cc)
        return _draft_dict(draft)

    @registry.register(
        "mail.send",
        mutating=True,
        description="Envoie un brouillon existant. Action sortante, toujours sous validation.",
    )
    async def _send(draft_id: str, idempotency_key: str = "", **_for_approval_summary) -> dict:
        # `_for_approval_summary` porte to/to_display/subject/body : nécessaires à
        # `format_approval_summary` (gate.py) avant l'envoi, pas à l'appel Gmail
        # lui-même (`drafts.send` ne prend que l'id du brouillon).
        message = await provider.send_draft(draft_id, idempotency_key)
        return _sent_dict(message)
