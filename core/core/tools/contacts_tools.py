"""Outils `contacts.resolve`/`contacts.get`, adossés à un vrai carnet Google.

`contacts.resolve` ne fait que relier le plan du modèle à `core/contacts.py` :
tout le calcul de score et la décision de désambiguïsation restent là-bas,
inchangés — ce module ne fait que fournir de vrais candidats.

`contacts.recent_interactions` reste absent : il faudrait miner l'historique
des messages (`core/db/models.py::Message`), pas le carnet d'adresses, et
c'est une fonctionnalité distincte qui n'a pas été demandée ici. Un plan qui
l'invoque le voit toujours écarté comme outil inconnu.
"""

from __future__ import annotations

from ..contacts import Contact, resolve
from .registry import ToolRegistry


def _contact_dict(contact: Contact) -> dict:
    return {
        "id": contact.id,
        "display_name": contact.display_name,
        "emails": contact.emails,
        "phones": contact.phones,
        "org": contact.org,
    }


def register_contacts_tools(registry: ToolRegistry, provider) -> None:
    """Enregistre `contacts.*` liés à ce provider — donc à cet utilisateur.

    Comme pour `register_calendar_tools`, un `ToolRegistry` neuf par job : le
    provider est fermé sur les identifiants Google d'un seul utilisateur.
    """

    @registry.register(
        "contacts.resolve",
        description="Résout un nom en contact. Désambiguïsation obligatoire si ambigu.",
    )
    async def _resolve(query: str, hint: str | None = None) -> dict:
        candidates = await provider.list_contacts()
        resolution = resolve(query, candidates, hint)
        return {
            "needs_disambiguation": resolution.needs_disambiguation,
            "reason": resolution.reason,
            "best": _contact_dict(resolution.best) if resolution.best else None,
            "options": [_contact_dict(c) for c in resolution.options()],
        }

    @registry.register("contacts.get", description="Détails d'un contact par id.")
    async def _get(contact_id: str) -> dict | None:
        candidates = await provider.list_contacts()
        match = next((c for c in candidates if c.id == contact_id), None)
        return _contact_dict(match) if match else None
