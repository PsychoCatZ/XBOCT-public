"""Programmatic Alembic entry points (sync, stdlib sqlite3)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from xboct.storage.db import database_path, sync_url

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_DIR = REPO_ROOT / "alembic"


class SchemaNotReady(RuntimeError):
    """Database schema is missing or not at the code's head revision."""


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", sync_url(database_url))
    config.attributes["configure_logging"] = False
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    path = database_path(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    if head is None:
        raise SchemaNotReady("no migrations found")
    return head


def known_revisions() -> set[str]:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return {rev.revision for rev in script.walk_revisions()}


def current_revision(database_url: str) -> str | None:
    path = database_path(database_url)
    if path is None or not path.exists():
        return None
    with closing(sqlite3.connect(path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def ensure_schema_current(database_url: str) -> None:
    current = current_revision(database_url)
    head = head_revision()
    if current != head:
        raise SchemaNotReady(
            f"database schema revision {current!r} != code head {head!r}; "
            "run migrations first (uv run xboct migrate)"
        )
