"""Résolution de contacts — source n°1 d'erreurs graves.

« Envoie un mail à Marc » n'est pas résoluble sans risque : s'il existe deux Marc,
choisir soi-même revient à envoyer un document au mauvais destinataire. Ce module
renvoie donc *toujours* une liste scorée, et impose la désambiguïsation quand le
meilleur candidat n'est pas franchement détaché.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

#: En dessous, le meilleur candidat n'est pas assez sûr pour agir seul.
MIN_TOP_SCORE = 0.85
#: En dessous, les deux premiers candidats sont trop proches pour trancher.
MIN_GAP = 0.15


@dataclass
class Contact:
    id: str
    display_name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    org: str | None = None
    aliases: list[str] = field(default_factory=list)
    last_interaction_at: datetime | None = None


@dataclass
class ContactMatch:
    contact: Contact
    score: float


@dataclass
class Resolution:
    matches: list[ContactMatch]
    needs_disambiguation: bool
    reason: str = ""

    @property
    def best(self) -> Contact | None:
        return self.matches[0].contact if self.matches else None

    def options(self, limit: int = 3) -> list[Contact]:
        """Les candidats à proposer à l'utilisateur — trois au maximum."""
        return [m.contact for m in self.matches[:limit]]


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def _name_score(query: str, candidate: str) -> float:
    q, c = _normalize(query), _normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    tokens = c.split()
    # « Marc » doit matcher fortement « Marc Dubois » sans l'égaler.
    if q in tokens:
        return 0.92 if len(tokens) > 1 else 1.0
    if c.startswith(q) or any(t.startswith(q) for t in tokens):
        return 0.80
    return SequenceMatcher(None, q, c).ratio()


def _recency_bonus(contact: Contact, now: datetime) -> float:
    """Départage deux homonymes par la fraîcheur de l'échange, sans jamais suffire
    à faire basculer une décision à lui seul."""
    if contact.last_interaction_at is None:
        return 0.0
    stamp = contact.last_interaction_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    days = max((now - stamp).days, 0)
    if days <= 7:
        return 0.05
    if days <= 30:
        return 0.03
    if days <= 180:
        return 0.01
    return 0.0


def resolve(
    query: str,
    candidates: list[Contact],
    hint: str | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Score les candidats et décide si une question à l'utilisateur est requise.

    `hint` est un indice contextuel libre (« celui de Vitagro », un domaine email) :
    il renforce un candidat dont l'organisation ou l'adresse correspond.
    """
    now = now or datetime.now(timezone.utc)

    # Une adresse email explicite n'est pas ambiguë.
    if "@" in query:
        for contact in candidates:
            if any(_normalize(e) == _normalize(query) for e in contact.emails):
                return Resolution([ContactMatch(contact, 1.0)], False)

    scored: list[ContactMatch] = []
    for contact in candidates:
        names = [contact.display_name, *contact.aliases]
        score = max(_name_score(query, n) for n in names)
        if hint:
            h = _normalize(hint)
            haystack = " ".join([contact.org or "", *contact.emails])
            if h and h in _normalize(haystack):
                score = min(score + 0.10, 1.0)
            else:
                # Écarter les non-concernés, sans quoi le plafond à 1.0 écrase
                # l'écart et l'indice ne départage jamais rien.
                score *= 0.85
        score = min(score + _recency_bonus(contact, now), 1.0)
        if score > 0.4:
            scored.append(ContactMatch(contact, round(score, 4)))

    scored.sort(key=lambda m: (-m.score, m.contact.display_name))

    if not scored:
        return Resolution([], True, f"aucun contact ne correspond à « {query} »")

    top = scored[0].score
    if top < MIN_TOP_SCORE:
        return Resolution(scored, True, f"meilleur score {top:.2f} < {MIN_TOP_SCORE}")

    if len(scored) > 1:
        gap = top - scored[1].score
        if gap < MIN_GAP:
            return Resolution(scored, True, f"écart {gap:.2f} < {MIN_GAP} entre les deux premiers")

    return Resolution(scored, False)


def format_disambiguation(query: str, resolution: Resolution) -> str:
    """Question posée à l'utilisateur — trois options numérotées au maximum."""
    if not resolution.matches:
        return f"Je ne trouve aucun contact pour « {query} ». Tu peux me donner l'adresse ?"

    lines = [f"Plusieurs contacts pour « {query} », lequel ?"]
    for i, contact in enumerate(resolution.options(), start=1):
        detail = contact.emails[0] if contact.emails else (contact.org or "")
        lines.append(f"{i}. {contact.display_name}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)
