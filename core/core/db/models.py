"""Schéma relationnel — la version durable des registres en mémoire.

`ChannelRegistry` et `ApprovalRegistry` marchaient en dictionnaires : correct
pour les tests, inutilisable en production, où le worker redémarre et où
plusieurs processus servent le même utilisateur. Un redémarrage perdait tous
les appairages et toutes les validations en attente.

Trois choix qui portent la sécurité du système, chacun exprimé comme une
contrainte de base plutôt que comme une convention de code :

- **Les jetons OAuth sont des `LargeBinary`**, jamais du texte : la colonne ne
  peut pas contenir un secret en clair par accident.
- **Une seule validation en attente par fil** — contrainte d'unicité partielle,
  pas une vérification applicative qu'une course pourrait contourner.
- **Un canal est unique par (kind, external_id)** : un même numéro ne peut pas
  être appairé à deux utilisateurs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    locale: Mapped[str] = mapped_column(String(16), default="fr")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    channels: Mapped[list[Channel]] = relationship(back_populates="user")


class Channel(Base):
    """Canal appairé. Un message venant d'ailleurs est ignoré en silence."""

    __tablename__ = "channels"
    __table_args__ = (
        # Un numéro ne peut appartenir qu'à un seul utilisateur : sans cette
        # contrainte, deux appairages concurrents créeraient une ambiguïté sur
        # qui donne les ordres.
        UniqueConstraint("kind", "external_id", name="uq_channel_identity"),
        Index("ix_channels_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # whatsapp | chrome | desktop | mobile
    external_id: Mapped[str] = mapped_column(String(128))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="channels")


class PairingCode(Base):
    """Code d'appairage à usage unique, à durée de vie courte."""

    __tablename__ = "pairing_codes"
    __table_args__ = (Index("ix_pairing_created", "created_at"),)

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Integration(Base):
    """Connexion OAuth. Les jetons sont chiffrés — jamais de colonne texte ici."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_integration_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))  # google | microsoft | notion
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # LargeBinary et non String : le type interdit d'y écrire un jeton en clair
    # sans que ça saute aux yeux en revue.
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (Index("ix_threads_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel_kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="open")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_thread", "thread_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    """Validation en attente — l'état qui doit survivre au redémarrage.

    Le gate suspend le plan et rend la main ; si cette ligne vit en mémoire, un
    déploiement pendant qu'un utilisateur réfléchit fait disparaître son « ok ».
    """

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_requested", "requested_at"),)

    # `thread_id` en clé primaire : une seule validation ouverte par fil, garantie
    # par la base. Une vérification applicative laisserait passer deux requêtes
    # concurrentes, et un « ok » validerait alors une action que l'utilisateur
    # croyait abandonnée.
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    plan: Mapped[list] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    completed: Mapped[list] = mapped_column(JSON, default=list)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_status", "status", "scheduled_for"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    intent: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    priority: Mapped[int] = mapped_column(SmallInteger, default=0)
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SeenMessage(Base):
    """Déduplication des webhooks, persistée.

    En mémoire, un redémarrage rouvre la porte aux doublons : Meta et Evolution
    retentent, et un message rejoué recrée le rendez-vous.
    """

    __tablename__ = "seen_messages"
    __table_args__ = (Index("ix_seen_created", "created_at"),)

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceToken(Base):
    """Jeton d'un client applicatif natif (Android/iOS/Windows) — pas WhatsApp.

    Émis une seule fois, à l'appairage, et montré en clair à cet instant-là
    uniquement : seul son hash est conservé, comme un mot de passe. Sans
    session ni login, ce jeton est la seule preuve d'identité que l'app
    présente sur `/app/messages` — un jeton en clair en base serait
    équivalent à stocker un mot de passe en clair.
    """

    __tablename__ = "device_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    """Dédup des opérations sans id imposable côté client (Gmail, contrairement à Calendar).

    `drafts.create`/`drafts.send` génèrent toujours un nouvel identifiant côté
    Google : sans cette table, un retry après timeout enverrait un second email
    identique. Elle joue le rôle qu'un en-tête `Idempotency-Key` jouerait si
    l'API le proposait.
    """

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    result_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Journal des actions. Non négociable : sans lui, aucune enquête possible."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16))  # agent | user | system
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_redacted: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
