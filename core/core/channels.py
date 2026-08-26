"""Appairage des canaux — qui a le droit de donner des ordres à l'agent.
Le webhook WhatsApp est public : n'importe qui connaissant le numéro du bot peut
lui écrire. Sans appairage, un inconnu pilote ton agenda et ta messagerie.
Le mécanisme : l'utilisateur génère un code à 6 chiffres dans le dashboard, puis
l'envoie au bot depuis le numéro à appairer. Le code est à usage unique et
expire vite.
Trois précautions, chacune contre une attaque concrète :
- comparaison à temps constant (un code à 6 chiffres se devine par timing)
- nombre d'essais borné (100 000 combinaisons se testent vite par force brute)
- code lié à un seul utilisateur, jamais réutilisable
"""
from __future__ import annotations
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
PAIRING_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
class PairingError(RuntimeError):
    """Appairage refusé. Le message est volontairement peu détaillé."""
@dataclass
class PairingCode:
    code: str
    user_id: str
    created_at: datetime
    attempts: int = 0
    consumed: bool = False
    def is_expired(self, now: datetime) -> bool:
        return now - self.created_at > PAIRING_TTL
@dataclass
class Channel:
    user_id: str
    kind: str  # whatsapp | chrome | desktop | mobile
    external_id: str  # numéro, device token
    verified_at: datetime
    active: bool = True
@dataclass
class ChannelRegistry:
    """Registre en mémoire. En production : tables `channels` et `pairing_codes`."""
    channels: dict[tuple[str, str], Channel] = field(default_factory=dict)
    pending: dict[str, PairingCode] = field(default_factory=dict)
    # --- côté dashboard ---------------------------------------------------
    def issue_code(self, user_id: str, now: datetime | None = None) -> str:
        """Génère un code à 6 chiffres. Un seul code actif par utilisateur."""
        now = now or datetime.now(timezone.utc)
        self._purge(now)
        for code, pending in list(self.pending.items()):
            if pending.user_id == user_id:
                del self.pending[code]
        # secrets, pas random : un générateur prévisible rendrait le code devinable.
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.pending[code] = PairingCode(code, user_id, now)
        return code
    # --- côté canal -------------------------------------------------------
    def redeem(
        self, submitted: str, kind: str, external_id: str, now: datetime | None = None
    ) -> Channel:
        """Consomme un code et enregistre le canal. Lève PairingError sinon."""
        now = now or datetime.now(timezone.utc)
        self._purge(now)
        submitted = submitted.strip().replace(" ", "")
        match = None
        for code, pending in self.pending.items():
            # Comparaison à temps constant sur tous les candidats : sortir tôt
            # sur le premier écart révélerait le code par mesure du temps.
            if hmac.compare_digest(code, submitted):
                match = pending
        if match is None:
            self._count_failure(now)
            raise PairingError("Code invalide ou expiré.")
        if match.consumed:
            raise PairingError("Code déjà utilisé.")
        match.attempts += 1
        if match.attempts > MAX_ATTEMPTS:
            del self.pending[match.code]
            raise PairingError("Trop de tentatives. Génère un nouveau code.")
        match.consumed = True
        del self.pending[match.code]
        channel = Channel(match.user_id, kind, external_id, now)
        self.channels[(kind, external_id)] = channel
        return channel
    def get_verified(self, kind: str, external_id: str) -> Channel | None:
        """Renvoie le canal appairé, ou None. Un None signifie : ignorer en silence."""
        channel = self.channels.get((kind, external_id))
        if channel is None or not channel.active:
            return None
        return channel
    def revoke(self, kind: str, external_id: str) -> bool:
        channel = self.channels.get((kind, external_id))
        if channel is None:
            return False
        channel.active = False
        return True
    # --- interne ----------------------------------------------------------
    def _purge(self, now: datetime) -> None:
        for code, pending in list(self.pending.items()):
            if pending.is_expired(now):
                del self.pending[code]
    def _count_failure(self, now: datetime) -> None:
        """Un mauvais code consomme un essai sur tous les codes en attente.
        Empêche de balayer l'espace des codes en profitant du fait qu'un échec
        sur un code ne coûte rien aux autres.
        """
        for pending in list(self.pending.values()):
            pending.attempts += 1
            if pending.attempts > MAX_ATTEMPTS:
                self.pending.pop(pending.code, None)
