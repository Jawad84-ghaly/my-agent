"""Moteur et sessions SQLAlchemy.

`expire_on_commit=False` est délibéré : sans lui, tout attribut lu après un
commit déclenche un rechargement, ce qui lève sur une session asynchrone déjà
fermée — le piège classique de l'async SQLAlchemy.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

DEFAULT_URL = "postgresql+asyncpg://nova:nova@localhost/nova"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None, echo: bool = False):
    url = url or database_url()
    kwargs: dict = {"echo": echo, "future": True}
    # SQLite (tests) ne connaît ni pool_size ni pre-ping.
    if not url.startswith("sqlite"):
        kwargs |= {"pool_size": 10, "max_overflow": 5, "pool_pre_ping": True}
    return create_async_engine(url, **kwargs)


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Une transaction par unité de travail : commit au succès, rollback sinon."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine) -> None:
    """Crée le schéma sans passer par Alembic. Tests et prototypage uniquement.

    En production, les migrations font foi : `create_all` ne sait pas faire
    évoluer une base existante, et l'utiliser masquerait une migration oubliée.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
