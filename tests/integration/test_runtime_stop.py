"""Stopping the runtime while a background task is inside a database transaction.

Regression: cooperative runtime shutdown — after a stop, the
next start could not take the write lock ("database is locked"). The checks below pause
a task deterministically inside a write transaction (events, no sleeps or timeouts) and
then stop the runtime.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event

from tests.integration.rt import OWNER
from xboct.application.service import Application
from xboct.scheduling.lock import AlreadyRunning, ProcessLock
from xboct.scheduling.runtime import Runtime
from xboct.storage.db import database_path
from xboct.storage.models import TelegramInbox
from xboct.telegram import inbox as inbox_module


def write_lock_free(db_url: str) -> bool:
    """True if another connection can take the write lock right now."""
    path = database_path(db_url)
    with closing(sqlite3.connect(path, timeout=0)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        conn.rollback()
        return True


def pause_point(monkeypatch, where: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Make one in-transaction step wait until released; `inside` tells it is there."""
    from xboct.application import intents
    from xboct.scheduling.outbox import OutboxSender

    inside, release = asyncio.Event(), asyncio.Event()

    def pausing(original):
        async def wrapper(*args, **kwargs):
            inside.set()
            await release.wait()
            return await original(*args, **kwargs)

        return wrapper

    if where == "poll":  # storing the fetched update (poll_once, BEGIN IMMEDIATE)
        monkeypatch.setattr(inbox_module, "set_state", pausing(inbox_module.set_state))
    elif where == "handle":  # answering the message (Application.run transaction)
        monkeypatch.setattr(intents, "answer_query", pausing(intents.answer_query))
    else:  # settling the sent reply (outbox transaction)
        monkeypatch.setattr(
            OutboxSender, "_finish_jobs", staticmethod(pausing(OutboxSender._finish_jobs))
        )
    return inside, release


@pytest.mark.parametrize("where", ["poll", "handle", "settle"])
@pytest.mark.parametrize("how", ["request_stop", "stop"])
async def test_stop_during_db_write_finishes_the_step_then_releases(bot, monkeypatch, where, how):
    await bot.stop()
    bot.provider.add("привет", {"kind": "query", "query": {"kind": "agenda"}})
    inside, release = pause_point(monkeypatch, where)
    bot.say("привет")
    app = await Application.open(bot.db_url, bot.clock)
    invalidated = []
    event.listen(app.engine.sync_engine, "invalidate", lambda *a: invalidated.append(a))
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await inside.wait()
    assert not write_lock_free(bot.db_url)  # a background task holds a write transaction

    stopping = None
    if how == "stop":
        stopping = asyncio.create_task(runtime.stop())
    else:
        runtime.request_stop()
    await asyncio.sleep(0)  # let stop() begin waiting
    # The step in progress is not cut off, and the process lock stays held until it ends.
    assert runtime.lock.held
    with pytest.raises(AlreadyRunning):
        ProcessLock(bot.lock).acquire()
    assert stopping is None or not stopping.done()  # stop() waits for the loop
    release.set()
    await task
    if stopping is not None:
        await stopping
    assert runtime.lock.held is False
    await app.close()
    assert invalidated == []  # no statement was interrupted
    assert write_lock_free(bot.db_url)  # no transaction or connection left behind
    monkeypatch.undo()
    await bot.start()  # a fresh runtime takes the process lock and writes normally
    await bot.tick()
    rows = await bot.rows(TelegramInbox)
    assert len(rows) == 1  # the update is neither lost nor duplicated
    assert rows[0].status == "done"


@pytest.mark.parametrize("release_first", [True, False])
async def test_stop_while_begin_waits_for_the_write_lock(bot, monkeypatch, release_first):
    """The incident: stop arrives while the poller's BEGIN IMMEDIATE is still executing
    in the aiosqlite worker thread (it waits for the write lock held elsewhere)."""
    import aiosqlite

    await bot.stop()
    parked = asyncio.Event()  # poller is inside get_updates
    resume = asyncio.Event()
    in_begin = asyncio.Event()  # poller's BEGIN IMMEDIATE was submitted to the thread
    armed = False

    class GatedTransport(type(bot.transport)):
        async def get_updates(self, offset, poll_seconds):
            parked.set()
            await resume.wait()
            return await super().get_updates(offset, poll_seconds)

    transport = bot.transport = GatedTransport()
    original_execute = aiosqlite.Cursor.execute

    async def watched_execute(self, sql, parameters=None):
        if armed and sql == "BEGIN IMMEDIATE":
            in_begin.set()
        return await original_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Cursor, "execute", watched_execute)
    app = await Application.open(bot.db_url, bot.clock)
    invalidated = []
    event.listen(app.engine.sync_engine, "invalidate", lambda *a: invalidated.append(a))
    runtime = Runtime(app, transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    await runtime.start()
    ticked = asyncio.Event()
    original_tick = runtime.tick

    async def tick_once(**kwargs):
        report = await original_tick(**kwargs)
        ticked.set()
        return report

    runtime.tick = tick_once  # type: ignore[method-assign]
    loop_task = asyncio.create_task(runtime.loop())
    await parked.wait()  # the poller waits on the network
    await ticked.wait()  # the first tick is over; the next one is 30 s away

    path = database_path(bot.db_url)
    holder = sqlite3.connect(path, timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # someone else holds the write lock
    bot.say("привет")
    armed = True
    resume.set()  # poller fetches and tries to store -> BEGIN IMMEDIATE waits in the thread
    await in_begin.wait()
    runtime.request_stop()
    if release_first:  # BEGIN succeeds in the thread just after the cancellation
        holder.rollback()
        await loop_task
    else:  # BEGIN is still blocked when the poller is cancelled
        await loop_task
        holder.rollback()
    holder.close()
    await app.close()
    assert runtime.lock.held is False
    assert write_lock_free(bot.db_url)
    # Our own shutdown never interrupts a statement: no connection was invalidated
    # (an invalidated aiosqlite connection is only closed later by its worker thread).
    assert invalidated == []


async def test_stop_while_session_close_rolls_back(bot, monkeypatch):
    """Stop while a read session of the poller is being closed (pool reset ROLLBACK)."""
    import aiosqlite

    await bot.stop()
    in_rollback, go_on = asyncio.Event(), asyncio.Event()
    armed = False
    original_rollback = aiosqlite.Connection.rollback

    async def paused_rollback(self):
        if armed:
            in_rollback.set()
            await go_on.wait()
        return await original_rollback(self)

    monkeypatch.setattr(aiosqlite.Connection, "rollback", paused_rollback)
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    await runtime.start()
    ticked = asyncio.Event()
    original_tick = runtime.tick

    async def tick_once(**kwargs):
        report = await original_tick(**kwargs)
        ticked.set()
        return report

    runtime.tick = tick_once  # type: ignore[method-assign]
    loop_task = asyncio.create_task(runtime.loop())
    await ticked.wait()
    armed = True  # the next ROLLBACK is the poller's read session closing
    await in_rollback.wait()
    runtime.request_stop()
    stopping = asyncio.create_task(loop_task_and_close(loop_task, app))
    go_on.set()
    await stopping
    assert runtime.lock.held is False
    assert write_lock_free(bot.db_url)
    monkeypatch.undo()
    await bot.start()


async def loop_task_and_close(loop_task, app):
    await loop_task
    await app.close()
