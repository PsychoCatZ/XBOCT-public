"""Shared fixtures: controlled clock, on-disk SQLite migrated by Alembic, fixture provider.

Every integration test gets its own database file (a copy of a once-migrated template),
so persistence is real; nothing is checked only in memory.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult
from xboct.application.intents import IntentService
from xboct.application.service import Application
from xboct.domain.clock import FixedClock
from xboct.llm.fixture import FixtureIntentProvider
from xboct.storage import migrate
from xboct.storage.db import sqlite_url
from xboct.storage.models import Base, NotificationJob

KRSK = ZoneInfo("Asia/Krasnoyarsk")
T = TypeVar("T", bound=Base)


def at(
    year: int, month: int, day: int, hour: int = 10, minute: int = 0, tz: ZoneInfo = KRSK
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "template.db"
    migrate.upgrade(sqlite_url(path))
    return path


@pytest.fixture
def db_path(tmp_path: Path, template_db: Path) -> Path:
    target = tmp_path / "xboct.db"
    with closing(sqlite3.connect(template_db)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return target


@pytest.fixture
def db_url(db_path: Path) -> str:
    return sqlite_url(db_path)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(at(2026, 10, 8, 10, 0))


@pytest.fixture
def provider() -> FixtureIntentProvider:
    return FixtureIntentProvider()


class Harness:
    """Thin test driver around the real Application (no shortcuts into handlers)."""

    def __init__(
        self, app: Application, clock: FixedClock, provider: FixtureIntentProvider
    ) -> None:
        self.app = app
        self.clock = clock
        self.provider = provider
        self.intents = IntentService(app, provider)
        self._n = 0

    def ctx(self, command_id: str | None = None, source: str = "test") -> CommandContext:
        self._n += 1
        return CommandContext(
            command_id=command_id or f"{source}:{self._n}",
            source=source,  # type: ignore[arg-type]
            received_at=self.clock.now(),
        )

    async def run(self, command: c.Command, command_id: str | None = None) -> CommandResult:
        return await self.app.execute(command, self.ctx(command_id))

    async def ok(self, command: c.Command, command_id: str | None = None) -> CommandResult:
        result = await self.run(command, command_id)
        assert result.status == "applied", result
        return result

    async def say(self, text: str, command_id: str | None = None) -> CommandResult:
        return await self.intents.handle_message(text, self.ctx(command_id, "telegram_update"))

    async def get(self, model: type[T], entity_id: Any) -> T:
        async with self.app.sessions() as session:
            entity = await session.get(model, entity_id)
            assert entity is not None
            return entity

    async def all(self, model: type[T], *where: Any) -> list[T]:
        async with self.app.sessions() as session:
            return list(await session.scalars(select(model).where(*where)))

    async def pending_jobs(self, subject_id: str) -> list[NotificationJob]:
        return await self.app.jobs(subject_id=subject_id, status="pending")

    async def restart(self) -> None:
        """Drop every connection and reopen the same database file."""
        url = str(self.app.engine.url)
        await self.app.close()
        self.app = await Application.open(url, self.clock)
        self.intents = IntentService(self.app, self.provider)


@pytest.fixture
async def x(
    db_url: str, clock: FixedClock, provider: FixtureIntentProvider
) -> AsyncIterator[Harness]:
    app = await Application.open(db_url, clock)
    harness = Harness(app, clock, provider)
    yield harness
    await harness.app.close()


def local(instant: datetime, tz: ZoneInfo = KRSK) -> datetime:
    return instant.astimezone(tz)


@pytest.fixture
async def bot(
    db_url: str, tmp_path: Path, clock: FixedClock, provider: FixtureIntentProvider
) -> AsyncIterator[Any]:
    from tests.integration.rt import Bot

    harness = Bot(db_url, tmp_path / "xboct.db.lock", clock, provider)
    await harness.start()
    yield harness
    await harness.stop()
