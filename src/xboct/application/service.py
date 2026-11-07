"""Application facade: the only entry point for mutations (web, Telegram, CLI, tests).

Each call is one short SQLite transaction (BEGIN IMMEDIATE) that writes the change,
its audit events, the revised notification jobs and the command receipt together.
A repeated command_id returns the stored result without re-applying anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult, Outcome
from xboct.application.handlers import apply_replans, dispatch
from xboct.application.uow import Uow
from xboct.domain.clock import Clock
from xboct.domain.errors import DomainError, NeedsClarification
from xboct.domain.timeutil import get_zone
from xboct.storage.db import create_engine, create_session_factory
from xboct.storage.migrate import ensure_schema_current
from xboct.storage.models import AppSettings, CommandReceipt, NotificationJob

DEFAULT_TIMEZONE = "Asia/Krasnoyarsk"


def default_settings(timezone: str, now: Any) -> AppSettings:
    """Starting defaults (docs/DECISIONS.md: «стартовый default», not user choices)."""
    get_zone(timezone)
    return AppSettings(
        id=1,
        timezone=timezone,
        quiet_start=time(23, 0),
        quiet_end=time(9, 0),
        digest_weekday=0,
        digest_time=time(10, 0),
        default_notice_time=time(10, 0),
        evening_time=time(19, 0),
        later_minutes=120,
        payment_advance_days=3,
        balance_threshold_days=5,
        overdue_followup_hours=24,
        daily_digest_enabled=False,
        max_auto_messages_per_day=4,
        profanity_enabled=True,
        version=1,
        updated_at=now,
    )


def payload_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def command_hash(command: c.Command) -> str:
    return payload_hash(command.model_dump(mode="json"))


Work = Callable[[Uow], Awaitable[Outcome]]


class Application:
    def __init__(
        self,
        engine: AsyncEngine,
        clock: Clock,
        *,
        default_timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        self.engine = engine
        self.sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        self.clock = clock
        self.default_timezone = default_timezone

    @classmethod
    async def open(
        cls, database_url: str, clock: Clock, *, default_timezone: str = DEFAULT_TIMEZONE
    ) -> Application:
        """Open an already migrated database. Refuses a schema behind the code."""
        ensure_schema_current(database_url)
        return cls(create_engine(database_url), clock, default_timezone=default_timezone)

    async def close(self) -> None:
        await self.engine.dispose()

    async def settings_in(self, session: AsyncSession) -> AppSettings:
        settings = await session.get(AppSettings, 1)
        if settings is None:
            settings = default_settings(self.default_timezone, self.clock.now())
            session.add(settings)
            await session.flush()
        return settings

    async def execute(self, command: c.Command, ctx: CommandContext) -> CommandResult:
        return await self.run(
            ctx, command.type, command_hash(command), lambda uow: dispatch(uow, command)
        )

    async def run(
        self, ctx: CommandContext, command_type: str, digest: str, work: Work
    ) -> CommandResult:
        async with self.sessions() as session, session.begin():
            receipt = await session.get(CommandReceipt, ctx.command_id)
            if receipt is not None:
                return self._replay(receipt, digest)
            settings = await self.settings_in(session)
            uow = Uow(session, ctx, self.clock.now(), settings)
            undoable = False
            try:
                async with session.begin_nested():
                    outcome = await work(uow)
                    await apply_replans(uow)
                result = CommandResult(
                    command_id=ctx.command_id,
                    command_type=command_type,
                    status=outcome.status,
                    message=outcome.message,
                    data=outcome.data,
                )
                undoable = outcome.undoable and outcome.status == "applied"
            except DomainError as exc:
                result = self._rejection(ctx, command_type, exc)
                if isinstance(exc, NeedsClarification):
                    for hook in uow.on_clarification:
                        await hook()
            session.add(
                CommandReceipt(
                    command_id=ctx.command_id,
                    source=ctx.source,
                    source_key=ctx.source_key,
                    command_type=command_type,
                    payload_hash=digest,
                    status=result.status,
                    result=result.model_dump(mode="json", exclude={"replayed"}),
                    received_at=ctx.received_at,
                    applied_at=uow.now,
                    undoable=undoable,
                )
            )
        return result

    @staticmethod
    def _replay(receipt: CommandReceipt, digest: str) -> CommandResult:
        if receipt.payload_hash != digest:
            return CommandResult(
                command_id=receipt.command_id,
                command_type=receipt.command_type,
                status="rejected",
                code="idempotency_conflict",
                message="Этот идентификатор команды уже использован для другого запроса.",
                replayed=True,
            )
        return CommandResult.model_validate({**receipt.result, "replayed": True})

    @staticmethod
    def _rejection(ctx: CommandContext, command_type: str, exc: DomainError) -> CommandResult:
        clarification = isinstance(exc, NeedsClarification)
        return CommandResult(
            command_id=ctx.command_id,
            command_type=command_type,
            status="clarification" if clarification else "rejected",
            code=exc.code,
            message=exc.message
            if clarification
            else f"{exc.message.rstrip('.')}. Данные не менял.",
            data={k: v for k, v in exc.details.items() if _jsonable(v)},
            options=exc.options if clarification else [],
        )

    async def refresh_schedule(self, command_id: str | None = None) -> CommandResult:
        """Extend occurrences/jobs as time passes. Called by the stage 2 scheduler."""
        from xboct.application.maintenance import refresh_schedule

        now = self.clock.now()
        ctx = CommandContext(
            command_id=command_id or f"system:refresh:{now.isoformat(timespec='seconds')}",
            source="system",
            received_at=now,
        )
        return await self.run(
            ctx, "refresh_schedule", payload_hash({"refresh": True}), refresh_schedule
        )

    # --- read side -------------------------------------------------------------------

    async def read(self, fn: Callable[[Uow], Awaitable[Any]]) -> Any:
        """Run a read-only function with a Uow (no receipt, no audit)."""
        async with self.sessions() as session, session.begin():
            settings = await self.settings_in(session)
            now = self.clock.now()
            ctx = CommandContext(command_id="read", source="system", received_at=now)
            return await fn(Uow(session, ctx, now, settings))

    async def jobs(
        self, subject_id: str | None = None, status: str | None = None
    ) -> list[NotificationJob]:
        async with self.sessions() as session:
            query = select(NotificationJob).order_by(NotificationJob.scheduled_at)
            if subject_id is not None:
                query = query.where(NotificationJob.subject_id == subject_id)
            if status is not None:
                query = query.where(NotificationJob.status == status)
            return list(await session.scalars(query))


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except TypeError:
        return False
    return True
