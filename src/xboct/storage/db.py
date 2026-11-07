"""Async SQLite engine. WAL, foreign keys, busy timeout, BEGIN IMMEDIATE writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

BUSY_TIMEOUT_MS = 5000


def sqlite_url(path: str | Path, *, driver: str = "aiosqlite") -> str:
    return f"sqlite+{driver}:///{Path(path).resolve()}"


def sync_url(database_url: str) -> str:
    """Alembic and maintenance tools use the stdlib sqlite3 driver."""
    return database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def database_path(database_url: str) -> Path | None:
    prefix = database_url.split("://", 1)
    if len(prefix) != 2 or not prefix[0].startswith("sqlite"):
        return None
    rest = prefix[1]
    if rest.startswith("/"):
        rest = rest[1:]
    return Path(rest) if rest and rest != ":memory:" else None


def apply_pragmas(dbapi_connection: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()


def create_engine(database_url: str) -> AsyncEngine:
    if not database_url.startswith("sqlite+aiosqlite://"):
        raise ValueError("XBOCT stage 1 supports only sqlite+aiosqlite:// URLs")
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Let SQLAlchemy emit BEGIN itself so every transaction takes the write lock
        # up front: no lock upgrades mid-transaction, no SQLITE_BUSY surprises.
        dbapi_connection.isolation_level = None
        apply_pragmas(dbapi_connection)

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=True)
