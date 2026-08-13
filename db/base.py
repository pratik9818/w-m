import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def init_engine(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def _ensure_engine() -> None:
    """Lazily init from DATABASE_URL if nothing has called init_engine() yet.

    Exists because serverless entrypoints (Vercel) don't reliably run FastAPI's
    lifespan hook on every cold start, but a fresh process always has the env var.
    """
    if _session_factory is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set and init_engine() was never called")
        init_engine(database_url)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    _ensure_engine()
    async with _session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """For use outside FastAPI's Depends() system, e.g. inside aiogram handlers."""
    _ensure_engine()
    async with _session_factory() as session:
        yield session
