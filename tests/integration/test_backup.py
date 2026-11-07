"""Backups (A29, stage 5): online snapshot, verification, retention, restore, runtime."""

from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import at
from xboct.application import commands as c
from xboct.cli import main
from xboct.storage import migrate
from xboct.storage.backup import (
    BackupError,
    create_snapshot,
    inspect_db,
    list_snapshots,
    prune,
    restore,
)
from xboct.storage.db import sqlite_url
from xboct.storage.models import NotificationJob, Task


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_snapshot_while_app_is_open_is_consistent_and_private(x, db_path, tmp_path):
    await x.ok(c.CreateTask(title="Секретная заметка", note="приватно", due_date=date(2026, 10, 9)))
    backups = tmp_path / "backups"
    snap = create_snapshot(db_path, backups, now=x.clock.now(), reason="manual")
    assert snap.path.name == "xboct-20261008T030000Z-manual.db"
    assert mode(backups) == 0o700
    assert mode(snap.path) == 0o600
    assert mode(snap.manifest_path) == 0o600
    m = snap.manifest
    assert m["integrity"] == "ok"
    assert m["schema_revision"] == migrate.head_revision()
    assert m["counts"]["task"] == 1
    assert m["counts"]["notification_job"] >= 1
    assert m["release"] == "dev"
    # The app keeps working; the snapshot does not see later writes.
    await x.ok(c.CreateTask(title="После копии"))
    assert inspect_db(snap.path)["counts"]["task"] == 1
    assert not list(backups.glob("*.partial"))


async def test_restore_into_new_path_keeps_jobs_receipts_history(x, db_path, tmp_path):
    result = await x.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 10)))
    snap = create_snapshot(db_path, tmp_path / "b", now=x.clock.now(), reason="manual")
    target = tmp_path / "restored" / "xboct.db"
    info = restore(snap.path, target, known_revisions=migrate.known_revisions())
    assert info["integrity"] == "ok"
    assert info["counts"] == snap.manifest["counts"]
    with closing(sqlite3.connect(target)) as conn:
        title = conn.execute("SELECT title FROM task WHERE id=?", (result.data["task_id"],))
        assert title.fetchone()[0] == "Счёт"
        assert conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] >= 1
    # The restored copy opens as a normal database at head.
    from xboct.application.service import Application

    app = await Application.open(sqlite_url(target), x.clock)
    try:
        async with app.sessions() as session:
            task = await session.get(Task, result.data["task_id"])
            assert task is not None
            jobs = (await session.execute(NotificationJob.__table__.select())).all()
            assert jobs
    finally:
        await app.close()


async def test_restore_refuses_existing_target_and_tampered_snapshot(x, db_path, tmp_path):
    snap = create_snapshot(db_path, tmp_path / "b", now=x.clock.now(), reason="manual")
    with pytest.raises(BackupError, match="already exists"):
        restore(snap.path, db_path, known_revisions=migrate.known_revisions())
    with snap.path.open("r+b") as handle:  # corrupt a byte
        handle.seek(200)
        handle.write(b"\xff")
    target = tmp_path / "new.db"
    with pytest.raises(BackupError, match="hash"):
        restore(snap.path, target, known_revisions=migrate.known_revisions())
    assert not target.exists()


def test_restore_rejects_unknown_schema(tmp_path):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db))
    snap = create_snapshot(
        db, tmp_path / "b", now=datetime(2026, 10, 8, tzinfo=UTC), reason="manual"
    )
    target = tmp_path / "r.db"
    with pytest.raises(BackupError, match="unknown schema revision"):
        restore(snap.path, target, known_revisions={"0001"})
    assert not target.exists()  # nothing half-trusted left behind


def test_retention_keeps_7_daily_and_4_weekly(tmp_path):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db))
    backups = tmp_path / "b"
    start = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
    for day in range(60):
        for hour in (1, 13):  # two snapshots a day
            create_snapshot(
                db, backups, now=start + timedelta(days=day, hours=hour - 1), reason="daily"
            )
    prune(backups)
    kept = [datetime.fromisoformat(s.manifest["created_at"]) for s in list_snapshots(backups)]
    days = {k.date() for k in kept}
    newest = max(days)
    assert {newest - timedelta(days=i) for i in range(7)} <= days
    weeks = {tuple(k.isocalendar())[:2] for k in kept}
    assert len(weeks) == 4
    assert len(kept) <= 7 + 4
    assert all(k.hour == 13 for k in kept)  # the newest of each day survives
    assert len(list(backups.glob("*.json"))) == len(kept)


async def test_runtime_makes_one_daily_backup_after_backup_time(bot, tmp_path):
    backups = tmp_path / "daily"
    bot.runtime.backup_dir = backups
    await bot.tick()  # 10:00 local, after the default 04:00
    assert len(list_snapshots(backups)) == 1
    await bot.tick()
    assert len(list_snapshots(backups)) == 1  # once per local day
    bot.clock.set(at(2026, 10, 9, 3, 0))
    await bot.tick()
    assert len(list_snapshots(backups)) == 1  # before 04:00
    bot.clock.set(at(2026, 10, 9, 4, 30))
    report = await bot.tick()
    assert report.backup is not None
    assert len(list_snapshots(backups)) == 2


async def test_runtime_backup_failure_is_reported_and_retried(bot, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    bot.runtime.backup_dir = blocker / "sub"
    report = await bot.tick()
    assert report.backup is None
    from xboct.scheduling.state import LAST_BACKUP, get_state

    async with bot.app.sessions() as session:
        state = await get_state(session, LAST_BACKUP)
    assert state["failed_at"]
    bot.runtime.backup_dir = tmp_path / "ok"
    await bot.tick()
    assert list_snapshots(tmp_path / "ok") == []  # retried only after an hour
    bot.clock.advance(hours=1, minutes=1)
    await bot.tick()
    assert len(list_snapshots(tmp_path / "ok")) == 1


# --- CLI ---------------------------------------------------------------------------------


def env(monkeypatch, db: Path, backups: Path | None = None, **extra: str) -> None:
    monkeypatch.setenv("DATABASE_URL", sqlite_url(db))
    if backups is not None:
        monkeypatch.setenv("BACKUP_DIR", str(backups))
    else:
        monkeypatch.delenv("BACKUP_DIR", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_cli_backup_and_restore(tmp_path, monkeypatch, capsys):
    db = tmp_path / "x.db"
    env(monkeypatch, db, tmp_path / "b")
    assert main(["migrate"]) == 0
    assert main(["backup"]) == 0
    out = capsys.readouterr().out
    assert "integrity=ok" in out
    [snap] = list_snapshots(tmp_path / "b")
    assert main(["restore", str(snap.path), "--to", str(tmp_path / "r.db")]) == 0
    out = capsys.readouterr().out
    assert "restored" in out
    assert "Do not run it with the production bot token" in out
    assert main(["restore", str(snap.path), "--to", str(db)]) == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_backup_needs_backup_dir(tmp_path, monkeypatch, capsys):
    env(monkeypatch, tmp_path / "x.db")
    assert main(["backup"]) == 2
    assert "BACKUP_DIR is not set" in capsys.readouterr().err


def test_migrate_snapshots_an_older_schema_first(tmp_path, monkeypatch, capsys):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db), "0003")
    env(monkeypatch, db, tmp_path / "b")
    assert main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "pre-migration backup" in out
    [snap] = list_snapshots(tmp_path / "b")
    assert snap.manifest["schema_revision"] == "0003"
    assert snap.manifest["reason"] == "pre-migration"
    assert migrate.current_revision(sqlite_url(db)) == migrate.head_revision()
    assert main(["migrate"]) == 0  # already at head: no extra snapshot
    assert len(list_snapshots(tmp_path / "b")) == 1


def test_production_migration_requires_backup_dir(tmp_path, monkeypatch, capsys):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db), "0003")
    env(monkeypatch, db, APP_ENV="production")
    assert main(["migrate"]) == 2
    assert "needs BACKUP_DIR" in capsys.readouterr().err
    assert migrate.current_revision(sqlite_url(db)) == "0003"


def test_manifest_is_plain_json_without_secrets(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db))
    monkeypatch.setenv("XBOCT_RELEASE", "abc1234")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    snap = create_snapshot(
        db, tmp_path / "b", now=datetime(2026, 10, 8, tzinfo=UTC), reason="manual"
    )
    text = snap.manifest_path.read_text()
    assert json.loads(text)["release"] == "abc1234"
    assert "secret-token" not in text


def test_migrate_refuses_while_the_app_holds_the_lock(tmp_path, monkeypatch, capsys):
    from xboct.scheduling.lock import ProcessLock
    from xboct.scheduling.runtime import lock_path_for

    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db), "0003")
    env(monkeypatch, db, tmp_path / "b")
    app_lock = ProcessLock(lock_path_for(sqlite_url(db)))  # what a running runtime holds
    app_lock.acquire()
    try:
        assert main(["migrate"]) == 2
        assert "stop the app before migrating" in capsys.readouterr().err
        assert migrate.current_revision(sqlite_url(db)) == "0003"  # untouched
        assert list_snapshots(tmp_path / "b") == []  # and no snapshot taken
    finally:
        app_lock.release()
    assert main(["migrate"]) == 0
    assert migrate.current_revision(sqlite_url(db)) == migrate.head_revision()


async def test_migrate_refuses_under_a_live_runtime(bot, monkeypatch, capsys):
    env(monkeypatch, Path(str(bot.app.engine.url).split("///", 1)[1]))
    assert main(["migrate"]) == 2
    assert "stop the app before migrating" in capsys.readouterr().err


def test_rollback_point_survives_later_daily_of_the_same_day(tmp_path):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db))
    backups = tmp_path / "b"
    day = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
    pre = create_snapshot(db, backups, now=day, reason="pre-migration")
    daily = create_snapshot(db, backups, now=day + timedelta(hours=3), reason="daily")
    removed = prune(backups)
    assert removed == []
    assert pre.path.exists()
    assert daily.path.exists()
    # Even after weeks of dailies the rollback point stays.
    for i in range(1, 60):
        create_snapshot(db, backups, now=day + timedelta(days=i), reason="daily")
    prune(backups)
    assert pre.path.exists()
    assert not daily.path.exists()  # ordinary rotation still applies to dailies


def test_last_three_rollback_points_of_each_type(tmp_path):
    db = tmp_path / "x.db"
    migrate.upgrade(sqlite_url(db))
    backups = tmp_path / "b"
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(5):
        for reason in ("pre-migration", "pre-release"):
            create_snapshot(db, backups, now=start + timedelta(hours=i), reason=reason)
    prune(backups)
    kept = [(s.manifest["reason"], s.manifest["created_at"]) for s in list_snapshots(backups)]
    for reason in ("pre-migration", "pre-release"):
        times = [t for r, t in kept if r == reason]
        assert len(times) == 3
        assert min(times) == (start + timedelta(hours=2)).isoformat()  # the newest three


async def test_web_only_process_also_blocks_migration(x, db_path, monkeypatch, capsys):
    from xboct.scheduling.runtime import lock_path_for
    from xboct.web.app import WebOptions, create_app
    from xboct.web.auth import AuthService

    lock = lock_path_for(sqlite_url(db_path))
    app = create_app(x.app, AuthService(x.app), WebOptions(secure_cookies=False, lock_path=lock))
    env(monkeypatch, db_path)
    async with app.router.lifespan_context(app):
        assert main(["migrate"]) == 2
        assert "stop the app before migrating" in capsys.readouterr().err
    assert main(["migrate"]) == 0  # released on shutdown
