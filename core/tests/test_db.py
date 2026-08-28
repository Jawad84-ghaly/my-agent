"""Tests de la couche de persistance, sur SQLite en mémoire.

Pas de Postgres requis : le schéma est portable, et ce qu'on vérifie ici — les
règles d'appairage, l'expiration des validations, le chiffrement des jetons —
ne dépend pas du dialecte.

Le dernier test est le plus important à long terme : il compare les modèles à la
migration et échoue si l'un a bougé sans l'autre. C'est l'oubli le plus banal et
le plus coûteux d'un projet avec migrations.
"""

import asyncio
import base64
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.approvals import PendingApproval
from core.channels import PairingError
from core.db.repositories import (
    PostgresApprovalRegistry,
    PostgresChannelRegistry,
    PostgresCredentialStore,
    PostgresSeenCache,
)
from core.db.session import create_all, make_engine, make_session_factory
from core.integrations.google_oauth import GoogleCredentials
from core.planning import Task
from core.security.crypto import CryptoError, generate_key
from core.store import UnknownIntegration

KEY = base64.urlsafe_b64decode(generate_key())
T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
CORE_DIR = Path(__file__).resolve().parents[1]


async def _with_session(body):
    """Base neuve par test : aucun état ne fuit d'un test à l'autre."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            await body(session)
            await session.commit()
    finally:
        await engine.dispose()


def run(body):
    asyncio.run(_with_session(body))


def seed_user(session, user_id="u1"):
    from core.db.models import User

    session.add(User(id=user_id, email=f"{user_id}@exemple.fr", display_name="Jawad"))


# --- appairage -------------------------------------------------------------

def test_pairing_survives_in_the_database():
    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        code = await reg.issue_code("u1", T0)
        channel = await reg.redeem(code, "whatsapp", "33612345678", T0)
        assert channel.user_id == "u1"
        found = await reg.get_verified("whatsapp", "33612345678")
        assert found is not None and found.user_id == "u1"

    run(body)


def test_unknown_number_is_not_verified():
    async def body(session):
        reg = PostgresChannelRegistry(session)
        assert await reg.get_verified("whatsapp", "33699999999") is None

    run(body)


def test_code_is_single_use():
    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        code = await reg.issue_code("u1", T0)
        await reg.redeem(code, "whatsapp", "33612345678", T0)
        with pytest.raises(PairingError):
            await reg.redeem(code, "whatsapp", "33600000000", T0)

    run(body)


def test_expired_code_is_refused():
    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        code = await reg.issue_code("u1", T0)
        with pytest.raises(PairingError):
            await reg.redeem(code, "whatsapp", "33612345678", T0 + timedelta(minutes=11))

    run(body)


def test_brute_force_burns_the_code():
    """La règle de la version mémoire doit survivre au portage."""

    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        code = await reg.issue_code("u1", T0)
        for _ in range(6):
            with pytest.raises(PairingError):
                await reg.redeem("999999", "whatsapp", "33612345678", T0)
        with pytest.raises(PairingError):
            await reg.redeem(code, "whatsapp", "33612345678", T0)

    run(body)


def test_new_code_invalidates_the_previous_one():
    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        first = await reg.issue_code("u1", T0)
        await reg.issue_code("u1", T0)
        with pytest.raises(PairingError):
            await reg.redeem(first, "whatsapp", "33612345678", T0)

    run(body)


def test_revoked_channel_stops_being_verified():
    async def body(session):
        seed_user(session)
        reg = PostgresChannelRegistry(session)
        code = await reg.issue_code("u1", T0)
        await reg.redeem(code, "whatsapp", "33612345678", T0)
        assert await reg.revoke("whatsapp", "33612345678") is True
        assert await reg.get_verified("whatsapp", "33612345678") is None

    run(body)


# --- validations -----------------------------------------------------------

def approval(thread="t1", at=T0) -> PendingApproval:
    plan = [
        Task("T1", "contacts.resolve", {"query": "Marc"}),
        Task("T2", "mail.send", {"draft_id": "d1"}, depends_on=("T1",)),
    ]
    return PendingApproval(
        thread_id=thread, user_id="u1", task=plan[1], plan=plan,
        results={"T1": {"email": "marc@exemple.fr"}}, completed=["T1"],
        summary="📤 Prêt à envoyer", requested_at=at,
    )


def test_approval_round_trips_with_its_plan():
    """La reprise a besoin du plan complet : le perdre rend le « ok » inutile."""

    async def body(session):
        reg = PostgresApprovalRegistry(session)
        await reg.put(approval())
        loaded = await reg.get("t1", T0 + timedelta(minutes=1))
        assert loaded is not None
        assert loaded.task.id == "T2"
        assert [t.id for t in loaded.plan] == ["T1", "T2"]
        assert loaded.results["T1"]["email"] == "marc@exemple.fr"
        assert loaded.completed == ["T1"]

    run(body)


def test_expired_approval_is_dropped():
    async def body(session):
        reg = PostgresApprovalRegistry(session)
        await reg.put(approval())
        assert await reg.get("t1", T0 + timedelta(minutes=31)) is None

    run(body)


def test_only_one_approval_per_thread():
    async def body(session):
        reg = PostgresApprovalRegistry(session)
        await reg.put(approval())
        await reg.put(approval(at=T0 + timedelta(minutes=2)))
        loaded = await reg.get("t1", T0 + timedelta(minutes=3))
        assert loaded is not None
        assert loaded.requested_at == T0 + timedelta(minutes=2)

    run(body)


def test_pop_removes_the_approval():
    async def body(session):
        reg = PostgresApprovalRegistry(session)
        await reg.put(approval())
        assert await reg.pop("t1", T0) is not None
        assert await reg.get("t1", T0) is None

    run(body)


def test_purge_removes_only_expired_ones():
    async def body(session):
        reg = PostgresApprovalRegistry(session)
        await reg.put(approval("old", T0 - timedelta(hours=2)))
        await reg.put(approval("fresh", T0))
        assert await reg.purge(T0) == 1
        assert await reg.get("fresh", T0) is not None

    run(body)


# --- déduplication ---------------------------------------------------------

def test_duplicate_webhook_is_detected_across_restarts():
    async def body(session):
        cache = PostgresSeenCache(session)
        assert await cache.check_and_add("MSG1", T0) is True
        # Une instance neuve simule un redémarrage du worker.
        assert await PostgresSeenCache(session).check_and_add("MSG1", T0) is False

    run(body)


def test_seen_entries_expire():
    async def body(session):
        cache = PostgresSeenCache(session)
        await cache.check_and_add("MSG1", T0)
        assert await cache.check_and_add("MSG1", T0 + timedelta(days=2)) is True

    run(body)


# --- jetons ----------------------------------------------------------------

def test_credentials_round_trip():
    async def body(session):
        seed_user(session)
        store = PostgresCredentialStore(session, KEY)
        await store.save("u1", GoogleCredentials("at-1", "rt-1", 1_800_000_000.0))
        loaded = await store.load("u1")
        assert loaded.access_token == "at-1"
        assert loaded.refresh_token == "rt-1"

    run(body)


def test_tokens_are_stored_encrypted():
    async def body(session):
        from core.db.models import Integration
        from sqlalchemy import select

        seed_user(session)
        await PostgresCredentialStore(session, KEY).save(
            "u1", GoogleCredentials("at-secret", "rt-secret", 0.0)
        )
        row = (await session.execute(select(Integration))).scalar_one()
        assert b"at-secret" not in row.access_token_enc
        assert b"rt-secret" not in row.refresh_token_enc

    run(body)


def test_row_of_another_user_cannot_be_decrypted():
    async def body(session):
        seed_user(session, "victime")
        seed_user(session, "attaquant")
        store = PostgresCredentialStore(session, KEY)
        await store.save("victime", GoogleCredentials("at", "rt", 0.0))

        from core.db.models import Integration
        from sqlalchemy import select

        row = (await session.execute(select(Integration))).scalar_one()
        session.add(
            Integration(
                user_id="attaquant", provider="google",
                access_token_enc=row.access_token_enc,
                refresh_token_enc=row.refresh_token_enc,
                expires_at=row.expires_at,
            )
        )
        await session.flush()
        with pytest.raises(CryptoError):
            await store.load("attaquant")

    run(body)


def test_saving_twice_updates_instead_of_duplicating():
    async def body(session):
        seed_user(session)
        store = PostgresCredentialStore(session, KEY)
        await store.save("u1", GoogleCredentials("at-1", "rt-1", 0.0))
        await store.save("u1", GoogleCredentials("at-2", "rt-1", 0.0))
        assert (await store.load("u1")).access_token == "at-2"

    run(body)


def test_missing_integration_is_explicit():
    async def body(session):
        with pytest.raises(UnknownIntegration, match="autoriser"):
            await PostgresCredentialStore(session, KEY).load("inconnu")

    run(body)


# --- migrations ------------------------------------------------------------

def test_migration_matches_the_models():
    """Échoue si les modèles ont bougé sans migration correspondante.

    C'est l'oubli le plus banal d'un projet avec Alembic, et il ne se voit qu'au
    déploiement — quand la base de production n'a pas la colonne attendue.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "check.db"
        env = {"DATABASE_URL": f"sqlite:///{db}", "PATH": "/usr/bin:/bin:/usr/local/bin"}

        up = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=CORE_DIR, env=env, capture_output=True, text=True,
        )
        assert up.returncode == 0, up.stderr

        check = subprocess.run(
            [sys.executable, "-m", "alembic", "check"],
            cwd=CORE_DIR, env=env, capture_output=True, text=True,
        )
        assert check.returncode == 0, (
            "Les modèles et la migration ont divergé. Génère la migration "
            f"manquante :\n  alembic revision --autogenerate -m '...'\n\n{check.stdout}{check.stderr}"
        )
