"""Consistent SQLite snapshots, retention and restore into a new path (docs/OPERATIONS.md).

- snapshot through the SQLite online backup API (safe while the app writes in WAL mode),
  never a plain file copy;
- every snapshot is checked with `PRAGMA integrity_check` and gets a manifest: schema
  revision, release, sha256, row counts of key tables;
- files are owner-only (0600; the directory 0700);
- retention: the newest snapshot of each of the last 7 days plus the newest of each of
  the last 4 ISO weeks; rollback points (pre-migration, pre-release) are kept apart,
  the last 3 of each type; anything else is deleted;
- restore only into a path that does not exist yet, then verify integrity, the manifest
  hash, the schema revision and the key tables. Nothing is started from a restored copy.

Snapshots contain private data (tasks, notes, payments): keep them off public places.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

PREFIX = "xboct-"
SUFFIX = ".db"
KEY_TABLES = (
    "task",
    "obligation",
    "occurrence",
    "payment",
    "balance_event",
    "notification_job",
    "command_receipt",
    "audit_event",
)
KEEP_DAILY = 7
KEEP_WEEKLY = 4
# Rollback points are rotated separately from daily/weekly snapshots.
PROTECTED_REASONS = ("pre-migration", "pre-release")
KEEP_PROTECTED = 3


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    path: Path
    manifest: dict[str, Any]

    @property
    def manifest_path(self) -> Path:
        return manifest_path(self.path)


def manifest_path(db_file: Path) -> Path:
    return db_file.with_suffix(".json")


def release() -> str:
    """Release id baked into the image (git commit); "dev" outside a release build."""
    return os.environ.get("XBOCT_RELEASE", "dev")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_db(path: Path) -> dict[str, Any]:
    """integrity_check, schema revision and key row counts of a database file."""
    uri = f"file:{path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        counts = {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for t in KEY_TABLES
            if t in tables
        }
    return {"integrity": integrity, "schema_revision": revision, "counts": counts}


def create_snapshot(db_file: Path, backup_dir: Path, *, now: datetime, reason: str) -> Snapshot:
    """Online backup of a live database into backup_dir, verified, owner-only."""
    if not db_file.exists():
        raise BackupError(f"database {db_file} does not exist")
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{PREFIX}{stamp}-{reason}{SUFFIX}"
    if target.exists():
        raise BackupError(f"{target.name} already exists")
    partial = target.with_name(target.name + ".partial")
    old_umask = os.umask(0o077)
    try:
        with (
            closing(sqlite3.connect(db_file)) as src,
            closing(sqlite3.connect(partial)) as dst,
        ):
            src.execute("PRAGMA busy_timeout=5000")
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file
        info = inspect_db(partial)
        if info["integrity"] != "ok":
            raise BackupError(f"integrity_check failed: {info['integrity']}")
        os.chmod(partial, 0o600)
        partial.replace(target)
        manifest = {
            "file": target.name,
            "created_at": now.astimezone(UTC).isoformat(),
            "reason": reason,
            "release": release(),
            "sha256": sha256(target),
            "size": target.stat().st_size,
            **info,
        }
        manifest_path(target).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        os.chmod(manifest_path(target), 0o600)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
    return Snapshot(target, manifest)


def list_snapshots(backup_dir: Path) -> list[Snapshot]:
    """Snapshots with a readable manifest, newest first."""
    found = []
    if not backup_dir.exists():
        return found
    for path in backup_dir.glob(f"{PREFIX}*{SUFFIX}"):
        try:
            manifest = json.loads(manifest_path(path).read_text())
        except (OSError, ValueError):
            continue
        found.append(Snapshot(path, manifest))
    found.sort(key=lambda s: s.manifest["created_at"], reverse=True)
    return found


def prune(
    backup_dir: Path,
    *,
    tz: tzinfo = UTC,
    keep_daily: int = KEEP_DAILY,
    keep_weekly: int = KEEP_WEEKLY,
    keep_protected: int = KEEP_PROTECTED,
) -> list[Path]:
    """Rotate regular snapshots; keep rollback points separately.

    Regular ones (daily, manual): the newest per local day (last N days) and per ISO week
    (last M weeks). Rollback points (pre-migration, pre-release) never take part in that
    rotation: the newest `keep_protected` of each type are kept on their own.
    """
    snapshots = list_snapshots(backup_dir)
    keep: set[Path] = set()
    days: set[str] = set()
    weeks: set[tuple[int, int]] = set()
    protected: dict[str, int] = {}
    for snap in snapshots:  # newest first
        reason = snap.manifest.get("reason", "")
        if reason in PROTECTED_REASONS:
            if protected.get(reason, 0) < keep_protected:
                protected[reason] = protected.get(reason, 0) + 1
                keep.add(snap.path)
            continue
        created = datetime.fromisoformat(snap.manifest["created_at"]).astimezone(tz)
        day = created.date().isoformat()
        week = tuple(created.isocalendar())[:2]
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(snap.path)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)  # type: ignore[arg-type]
            keep.add(snap.path)
    removed = []
    for snap in snapshots:
        if snap.path not in keep:
            snap.path.unlink(missing_ok=True)
            snap.manifest_path.unlink(missing_ok=True)
            removed.append(snap.path)
    return removed


def restore(snapshot: Path, target: Path, *, known_revisions: set[str]) -> dict[str, Any]:
    """Copy a verified snapshot into a NEW path and check what came out."""
    if target.exists():
        raise BackupError(f"{target} already exists: restore only into a new path")
    manifest_file = manifest_path(snapshot)
    if not manifest_file.exists():
        raise BackupError(f"manifest {manifest_file.name} is missing")
    manifest = json.loads(manifest_file.read_text())
    if sha256(snapshot) != manifest.get("sha256"):
        raise BackupError("snapshot hash does not match its manifest")
    target.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        with (
            closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(target)) as dst,
        ):
            src.backup(dst)
    finally:
        os.umask(old_umask)
    info = inspect_db(target)
    problems = []
    if info["integrity"] != "ok":
        problems.append(f"integrity_check: {info['integrity']}")
    if info["schema_revision"] not in known_revisions:
        problems.append(f"unknown schema revision {info['schema_revision']!r}")
    if info["counts"] != manifest.get("counts"):
        problems.append("row counts differ from the manifest")
    if problems:
        target.unlink(missing_ok=True)  # never leave a half-trusted copy behind
        raise BackupError("; ".join(problems))
    return {"manifest": manifest, **info}
