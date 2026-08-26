"""Configuration Alembic — URL prise dans l'environnement, jamais dans le .ini.

Le driver asyncpg n'est pas utilisable par Alembic en mode synchrone : on le
remplace par psycopg pour la durée de la migration. Sans cette substitution,
`alembic upgrade` échoue avec une erreur de driver peu parlante.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from core.db.models import Base
from core.db.session import database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def sync_url() -> str:
    url = database_url()
    return url.replace("+asyncpg", "+psycopg").replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
