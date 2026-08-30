import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from db.base import Base
from db.models import *  # noqa: F401,F403  -- registers all models on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    # Fall back to the app's own settings, which read .env. Alembic is normally run by
    # hand from a shell that has never exported DATABASE_URL, and without this the
    # failure is a bare `KeyError: 'url'` that says nothing about the missing variable.
    from bot_api.config import get_settings

    database_url = get_settings().database_url
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # statement_cache_size=0: required against Supabase's Supavisor pgbouncer
    # transaction-mode pooler, same as db/base.py's engine -- prepared statements
    # don't survive across pooled connections otherwise.
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"statement_cache_size": 0},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
