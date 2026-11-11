"""Async database layer for Phase 2 (reservations, agents, audit).

Uses SQLAlchemy 2.0 async. In production it talks to Heroku Postgres via
DATABASE_URL; with no DATABASE_URL it falls back to a local SQLite file so tests
and local dev run with zero setup. Heroku hands out `postgres://…` URLs, which we
normalise to the async driver form `postgresql+asyncpg://…`.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger("carfinder.db")


class Base(DeclarativeBase):
    pass


def _normalise(url: str) -> str:
    if not url:
        return "sqlite+aiosqlite:///./carfinder.db"
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    # asyncpg doesn't take libpq's ?sslmode=; strip it (Heroku appends it).
    if "+asyncpg" in url and "sslmode=" in url:
        base, _, _q = url.partition("?")
        url = base
    return url


DATABASE_URL = _normalise(os.environ.get("DATABASE_URL", ""))

_engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
_Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def _missing_column_ddl(sync_conn) -> list[str]:
    """Return ALTER statements for columns our models define but the DB lacks.
    A lightweight auto-migrate so adding a column to a model doesn't require a
    manual migration (SQLAlchemy's create_all never ALTERs existing tables)."""
    insp = inspect(sync_conn)
    dialect = sync_conn.dialect
    out: list[str] = []
    for table in Base.metadata.sorted_tables:
        try:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            if col.name not in existing:
                coltype = col.type.compile(dialect=dialect)
                out.append(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}')
    return out


async def init_db() -> None:
    """Create missing tables, then add any missing columns (idempotent auto-migrate)."""
    from . import models_db  # noqa: F401  (register models on Base.metadata)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # add columns the models have but the existing tables don't (each in its own txn,
    # so one failure can't block the others)
    async with _engine.connect() as conn:
        ddls = await conn.run_sync(_missing_column_ddl)
    for ddl in ddls:
        try:
            async with _engine.begin() as conn:
                await conn.exec_driver_sql(ddl)
            logger.info("auto-migrate: %s", ddl)
        except Exception as exc:
            logger.warning("auto-migrate skipped (%s): %s", ddl, exc)


def session_factory() -> async_sessionmaker[AsyncSession]:
    return _Session


async def get_session():
    """FastAPI dependency: yields an AsyncSession and closes it."""
    async with _Session() as session:
        yield session
