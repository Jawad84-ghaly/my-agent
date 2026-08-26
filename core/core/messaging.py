"""Formatage et envoi sortants — adapter la réponse au canal.

WhatsApp n'est pas un terminal : pas de tableaux, pas de titres, une syntaxe de
gras qui lui est propre, et une limite dure à 4096 caractères par message. Une
réponse pensée pour le dashboard y devient illisible.

Le découpage se fait sur les frontières naturelles du texte — paragraphe, puis
ligne, puis phrase — parce qu'une coupure au milieu d'un mot dans un message de
confirmation donne l'impression que l'agent a planté.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

WHATSAPP_LIMIT = 4096
SAFE_CHUNK = 3900  # marge pour le suffixe de pagination


class Sender(Protocol):
    async def send_text(self, to: str, body: str) -> None: ...


def to_whatsapp(markdown: str) -> str:
    """Convertit le markdown courant vers la syntaxe WhatsApp.

    `**gras**` → `*gras*`, `*italique*` → `_italique_`. Les titres deviennent du
    gras, les tableaux sont aplatis : WhatsApp les rend en bouillie sinon.
    """
    text = markdown.strip()

    # Protéger les blocs de code avant toute substitution.
    blocks: list[str] = []

    def stash(match: re.Match) -> str:
        blocks.append(match.group(0))
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```.*?```", stash, text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", stash, text)

    # Titres et gras passent par le marqueur § : écrire directement `*...*` ici
    # ferait retomber le résultat dans la règle d'italique juste en dessous.
    text = re.sub(r"^#{1,6}\s*(.+)$", r"§\1§", text, flags=re.M)   # titres → gras
    text = re.sub(r"\*\*(.+?)\*\*", r"§\1§", text)                  # gras md → marqueur
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)  # italique
    text = text.replace("§", "*")                                   # marqueur → gras WA
    text = re.sub(r"^\s*[-•]\s+", "• ", text, flags=re.M)           # puces
    text = re.sub(r"^\s*\|.*\|\s*$", _flatten_row, text, flags=re.M)  # tableaux
    text = re.sub(r"^\s*\|?[-: |]+\|?\s*$", "", text, flags=re.M)   # séparateurs
    text = re.sub(r"\n{3,}", "\n\n", text)

    for i, block in enumerate(blocks):
        text = text.replace(f"\x00{i}\x00", block.replace("```", "```"))
    return text.strip()


def _flatten_row(match: re.Match) -> str:
    cells = [c.strip() for c in match.group(0).strip().strip("|").split("|")]
    return " — ".join(c for c in cells if c)


def split_message(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Découpe sur les frontières naturelles, jamais au milieu d'un mot."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit  # mot unique plus long que la limite : coupure forcée
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)

    total = len(chunks)
    return [f"{c}\n({i}/{total})" for i, c in enumerate(chunks, 1)]


@dataclass
class ChannelFormatter:
    """Applique les règles de longueur et de format propres à chaque canal."""

    channel: str

    def render(self, markdown: str) -> list[str]:
        if self.channel == "whatsapp":
            return split_message(to_whatsapp(markdown))
        # Desktop, mobile et Chrome affichent du markdown riche.
        return [markdown.strip()]


@dataclass
class EvolutionSender:
    """Envoi via Evolution API. Le transport est injecté, donc testable."""

    transport: object
    base_url: str
    instance: str
    api_key: str

    async def send_text(self, to: str, body: str) -> None:
        from .integrations.http import request_with_retry

        await request_with_retry(
            self.transport,
            "POST",
            f"{self.base_url}/message/sendText/{self.instance}",
            headers={"apikey": self.api_key},
            json={"number": to, "text": body},
        )

    async def send_typing(self, to: str) -> None:
        """Indicateur « en train d'écrire » : l'utilisateur sait que ça travaille."""
        from .integrations.http import request_with_retry

        await request_with_retry(
            self.transport,
            "POST",
            f"{self.base_url}/chat/sendPresence/{self.instance}",
            headers={"apikey": self.api_key},
            json={"number": to, "presence": "composing"},
        )


@dataclass
class RecordingSender:
    """Double de test : conserve ce qui aurait été envoyé."""

    sent: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))

    async def send_typing(self, to: str) -> None:
        pass
