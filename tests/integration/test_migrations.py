import sqlite3
from contextlib import closing
from datetime import date

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from tests.conftest import Harness
from xboct.application import commands as c
from xboct.application.service import Application
from xboct.storage import migrate
from xboct.storage.db import sqlite_url, sync_url
from xboct.storage.models import Base, CommandReceipt, NotificationJob, Task

TABLES = {
    "audit_event",
    "balance_event",
    "delivery",
    "digest_coverage",
    "digest_run",
    "outbox_message",
    "runtime_state",
    "telegram_inbox",
    "command_receipt",
    "notification_job",
    "obligation",
    "occurrence",
    "payment",
    "pending_intent",
    "recurrence",
    "settings",
    "task",
}


def test_upgrade_from_empty_matches_models(tmp_path):
    # A29: migrations create exactly the schema the code expects.
    url = sqlite_url(tmp_path / "fresh.db")
    assert migrate.current_revision(url) is None
    migrate.upgrade(url)
    assert migrate.current_revision(url) == migrate.head_revision()
    engine = create_engine(sync_url(url))
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= TABLES
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_and_upgrade_again(tmp_path):
    url = sqlite_url(tmp_path / "cycle.db")
    migrate.upgrade(url)
    migrate.downgrade(url, "base")
    with closing(sqlite3.connect(tmp_path / "cycle.db")) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & TABLES
    migrate.upgrade(url)
    assert migrate.current_revision(url) == migrate.head_revision()


async def test_app_refuses_unmigrated_database(tmp_path, clock):
    with pytest.raises(migrate.SchemaNotReady):
        await Application.open(sqlite_url(tmp_path / "empty.db"), clock)


async def test_pragmas_wal_and_foreign_keys(x):
    async with x.app.engine.connect() as conn:
        assert (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar() == "wal"
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1


async def test_consistent_copy_to_new_path_keeps_data(x, db_path, tmp_path, clock, provider):
    # A29 (stage 1 part): an online SQLite backup into a new path is readable with
    # jobs, receipts and history. Scheduled backups/restore scripts come in stage 5.
    await x.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)), command_id="k1")
    target = tmp_path / "restored" / "xboct.db"
    target.parent.mkdir()
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored = Harness(await Application.open(sqlite_url(target), clock), clock, provider)
    try:
        assert [t.title for t in await restored.all(Task)] == ["Ответить Юле"]
        assert len(await restored.all(NotificationJob)) == 2
        assert (await restored.get(CommandReceipt, "k1")).status == "applied"
        replay = await restored.run(
            c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)), "k1"
        )
        assert replay.replayed
    finally:
        await restored.app.close()
