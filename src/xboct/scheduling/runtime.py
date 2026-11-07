"""The single background runtime: polling, inbox processing, scheduler and sender.

One process, one instance per data volume (OS file lock). `tick()` is deterministic
and is what tests drive with a fixed clock; `run()` repeats it every 30 seconds while a
separate task long-polls Telegram. State lives in SQLite, never only in memory:
restart = start() again, and everything continues from the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from xboct.application.context import CommandContext
from xboct.application.intents import DEFAULT_DAILY_CALL_LIMIT, IntentService
from xboct.application.service import Application
from xboct.application.uow import Uow
from xboct.domain.timeutil import get_zone, local_date
from xboct.llm.provider import IntentProvider
from xboct.scheduling.digest import maybe_create_digest
from xboct.scheduling.lock import ProcessLock
from xboct.scheduling.notifier import dispatch_due, quiet_end_after, verify_outbox
from xboct.scheduling.outbox import OutboxSender
from xboct.scheduling.state import (
    DIGEST_SINCE,
    LAST_BACKUP,
    LAST_REFRESH,
    LAST_TICK,
    TELEGRAM_HEALTH,
    get_state,
    set_state,
)
from xboct.storage.backup import create_snapshot, prune
from xboct.storage.db import database_path
from xboct.storage.models import OutboxMessage
from xboct.telegram.bot import BotHandler
from xboct.telegram.inbox import poll_once, process_pending
from xboct.telegram.transport import Transport, TransportError
from xboct.web.auth import AuthService

log = logging.getLogger(__name__)

TICK_SECONDS = 30
DEFAULT_BACKUP_TIME = time(4, 0)
BACKUP_RETRY = timedelta(hours=1)
POLL_TIMEOUT_SECONDS = 25


@dataclass
class TickReport:
    refreshed: bool = False
    polled: int = 0
    processed: int = 0
    digest: str | None = None
    dispatched: int = 0
    stale: int = 0
    deferred: int = 0
    suppressed: int = 0
    sent: int = 0
    backup: str | None = None
    errors: list[str] = field(default_factory=list)


def lock_path_for(database_url: str) -> Path:
    path = database_path(database_url)
    if path is None:
        raise ValueError("process lock needs a file database")
    return path.with_name(path.name + ".lock")


class Runtime:
    def __init__(
        self,
        app: Application,
        transport: Transport,
        provider: IntentProvider,
        owner_id: int,
        *,
        lock_path: Path | None = None,
        poll_timeout: int = POLL_TIMEOUT_SECONDS,
        daily_call_limit: int = DEFAULT_DAILY_CALL_LIMIT,
        auth: AuthService | None = None,
        backup_dir: Path | None = None,
        backup_time: time = DEFAULT_BACKUP_TIME,
    ) -> None:
        self.backup_dir = backup_dir
        self.backup_time = backup_time
        self.app = app
        self.transport = transport
        self.owner_id = owner_id
        self.poll_timeout = poll_timeout
        self.lock = ProcessLock(lock_path or lock_path_for(str(app.engine.url)))
        self.intents = IntentService(app, provider, daily_call_limit=daily_call_limit)
        self.bot = BotHandler(app, self.intents, transport, owner_id, auth=auth)
        self.sender = OutboxSender(
            app.sessions, transport, app.clock.now, verify=self._verify, quiet_gate=self._quiet
        )
        self._stop = asyncio.Event()
        self._started = False
        self._looping = False
        self._loop_done = asyncio.Event()
        # Two independent locks. `_inbox` serializes owner updates (in update_id order) and
        # may wait for the language model for a while; `_outbox` covers the short
        # scheduler/sender steps. A slow model therefore never stops reminders, the digest
        # or sending; no DB transaction is open while the model is thinking.
        self._inbox = asyncio.Lock()
        self._outbox = asyncio.Lock()

    # --- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.lock.acquire()  # raises AlreadyRunning: no second poller/scheduler
        try:
            await self._mark_first_start()
            await self.sender.reclaim(all_sending=True)
            await self.refresh(startup=True)
        except Exception:
            self.lock.release()
            raise
        self._started = True

    async def stop(self) -> None:
        """Stop and release the process lock only after background work has finished."""
        self._stop.set()
        if self._looping:
            await self._loop_done.wait()  # loop() releases the lock at its very end
        else:
            self._release()

    def request_stop(self) -> None:
        """Ask loop() to finish; loop() itself releases the lock."""
        self._stop.set()

    def _release(self) -> None:
        self.lock.release()
        self._started = False

    async def run(self) -> None:
        """Standalone production loop (`xboct run`). Not used by automatic tests."""
        await self.start()
        await self.loop()

    async def loop(self) -> None:
        """Ticks and polling until stop(); `start()` must have succeeded (web lifespan).

        Shutdown is cooperative: only waiting (the Telegram long poll, idle pauses) is
        interrupted. A database statement is never cancelled midway — that would
        invalidate its connection and leave closing it to the driver's worker thread
        (regression covered by test_runtime_stop.py). The process lock is
        released only after every background task has finished.
        """
        self._looping = True
        self._loop_done.clear()
        poller = asyncio.create_task(self._poll_loop())
        try:
            while not self._stop.is_set():
                await self.tick(poll=False)
                await self._idle(TICK_SECONDS)
        finally:
            self._stop.set()
            await poller  # finishes its current step (no DB work is cut off), then exits
            self._release()
            self._looping = False
            self._loop_done.set()

    async def _idle(self, seconds: float) -> None:
        """Wait, but wake up at once when a stop is requested."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                stored = await poll_once(
                    self.app.sessions,
                    self.transport,
                    self.app.clock.now,
                    self.owner_id,
                    poll_seconds=self.poll_timeout,
                    stop=self._stop,
                )
                if stored:
                    await self.process_inbox(wait=True)
                await self._health(ok=True)
                if not stored:
                    await self._idle(1)  # long polling normally waits server-side
            except TransportError as exc:
                await self._health(ok=False, error=exc.kind)
                await self._idle(max(exc.retry_after, 5))
            except Exception:
                log.exception("poll loop error")
                await self._idle(5)

    # --- one deterministic pass ------------------------------------------------------

    async def tick(self, *, poll: bool = True) -> TickReport:
        if not self._started:
            raise RuntimeError("Runtime.start() first")
        report = TickReport()
        report.refreshed = await self.refresh()
        if poll:
            try:
                report.polled = await poll_once(
                    self.app.sessions,
                    self.transport,
                    self.app.clock.now,
                    self.owner_id,
                    poll_seconds=0,
                )
                await self._health(ok=True)
            except TransportError as exc:
                report.errors.append(f"poll:{exc.kind}")
                await self._health(ok=False, error=exc.kind)
        report.processed = await self.process_inbox(wait=False)
        async with self._outbox:
            await self.sender.reclaim()  # safety net: expired leases only
            # Digest first and sent right away: only a CONFIRMED digest may suppress ordinary
            # same-day duplicates dispatched below; a failed digest suppresses nothing.
            async with self.app.sessions() as session, session.begin():
                report.digest = await maybe_create_digest(await self._uow(session), self.owner_id)
            report.sent = await self.sender.send_due(stop=self._stop)
            await self._dispatch(report)
            report.sent += await self.sender.send_due(stop=self._stop)
        report.backup = await self.maybe_backup()
        async with self.app.sessions() as session, session.begin():
            await set_state(
                session, LAST_TICK, {"at": self.app.clock.now().isoformat()}, self.app.clock.now()
            )
        return report

    async def maybe_backup(self) -> str | None:
        """Daily snapshot after BACKUP_TIME local; a failure is retried an hour later."""
        if self.backup_dir is None:
            return None
        now = self.app.clock.now()
        async with self.app.sessions() as session, session.begin():
            settings = await self.app.settings_in(session)
            state = await get_state(session, LAST_BACKUP) or {}
        tz = get_zone(settings.timezone)
        local = now.astimezone(tz)
        if local.time() < self.backup_time or state.get("local_date") == local.date().isoformat():
            return None
        failed_at = state.get("failed_at")
        if failed_at and now - datetime.fromisoformat(failed_at) < BACKUP_RETRY:
            return None
        db_file = database_path(str(self.app.engine.url))
        assert db_file is not None
        try:
            snapshot = await asyncio.to_thread(
                create_snapshot, db_file, self.backup_dir, now=now, reason="daily"
            )
            await asyncio.to_thread(prune, self.backup_dir, tz=tz)
        except Exception as exc:  # reported in diagnostics, retried later
            log.exception("daily backup failed")
            state.update(failed_at=now.isoformat(), error=type(exc).__name__)
            result = None
        else:
            state = {
                "local_date": local.date().isoformat(),
                "at": now.isoformat(),
                "file": snapshot.path.name,
            }
            result = snapshot.path.name
        async with self.app.sessions() as session, session.begin():
            await set_state(session, LAST_BACKUP, state, now)
        return result

    async def process_inbox(self, *, wait: bool) -> int:
        """Handle stored owner updates, then send their replies.

        wait=False (scheduler tick): if another worker is already handling updates —
        possibly waiting for the model — skip instead of blocking the scheduler.
        """
        if not wait and self._inbox.locked():
            return 0
        async with self._inbox:
            processed = await process_pending(
                self.app.sessions, self.bot, self.app.clock.now, stop=self._stop
            )
        if processed:
            async with self._outbox:
                # After a stop request no delivery is started: the replies are already
                # saved in the outbox and go out after the next start. A send that is
                # already in flight finishes (it is the one Telegram send in the budget).
                await self.sender.send_due(stop=self._stop)
        return processed

    async def refresh(self, *, startup: bool = False) -> bool:
        """refresh_schedule() at startup and once per local calendar day of the owner.

        The local date of the last SUCCESSFUL refresh is stored; after a failure the next
        tick tries again. User changes are replanned immediately by their own commands.
        """
        now = self.app.clock.now()
        async with self.app.sessions() as session:
            settings = await self.app.settings_in(session)
            today = local_date(
                now, Uow(session, CommandContext("r", "system", now), now, settings).tz
            )
            state = await get_state(session, LAST_REFRESH)
        if not startup and state is not None and state.get("local_date") == today.isoformat():
            return False
        command_id = (
            f"system:refresh:start:{now.isoformat(timespec='seconds')}"
            if startup
            else f"system:refresh:{today.isoformat()}"
        )
        try:
            result = await self.app.refresh_schedule(command_id=command_id)
        except Exception:
            log.exception("refresh_schedule failed; will retry next tick")
            return False
        if result.status != "applied":
            return False
        async with self.app.sessions() as session, session.begin():
            await set_state(
                session, LAST_REFRESH, {"local_date": today.isoformat(), "at": now.isoformat()}, now
            )
        return True

    async def _dispatch(self, report: TickReport) -> None:
        async with self.app.sessions() as session, session.begin():
            uow = await self._uow(session)
            stats = await dispatch_due(uow, self.owner_id, digest_outbox_id=report.digest)
        report.dispatched = len(stats.messages)
        report.stale = stats.stale
        report.deferred = stats.deferred
        report.suppressed = stats.suppressed

    # --- helpers ---------------------------------------------------------------------

    async def _mark_first_start(self) -> None:
        now = self.app.clock.now()
        async with self.app.sessions() as session, session.begin():
            if await get_state(session, DIGEST_SINCE) is None:
                await set_state(session, DIGEST_SINCE, {"at": now.isoformat()}, now)

    async def _uow(self, session: AsyncSession) -> Uow:
        settings = await self.app.settings_in(session)
        now = self.app.clock.now()
        return Uow(session, CommandContext("scheduler", "system", now), now, settings)

    async def _verify(self, session: AsyncSession, message: OutboxMessage, now: datetime) -> bool:
        return await verify_outbox(await self._uow(session), message)

    async def _quiet(self, session: AsyncSession, now: datetime) -> datetime | None:
        return quiet_end_after(await self._uow(session), now)

    async def _health(self, *, ok: bool, error: str | None = None) -> None:
        now = self.app.clock.now()
        async with self.app.sessions() as session, session.begin():
            state = await get_state(session, TELEGRAM_HEALTH) or {}
            if ok:
                state["last_ok"] = now.isoformat()
            else:
                state["last_error"] = {"at": now.isoformat(), "kind": error}
            await set_state(session, TELEGRAM_HEALTH, state, now)
