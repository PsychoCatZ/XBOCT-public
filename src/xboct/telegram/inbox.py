"""Telegram inbox: store first, acknowledge second, process with recovery.

1. getUpdates(offset) -> rows are inserted (duplicates ignored) and the next offset is
   saved in ONE transaction. Telegram treats an update as acknowledged only when a later
   getUpdates passes a larger offset, which happens after this commit.
2. Rows in `received` or `processing` (left by a crash) are processed in update_id
   order. A crash after the application commit but before the row is marked done is
   safe: the command receipt returns the stored result and the reply outbox entry has a
   dedup key, so nothing is applied or answered twice.
Updates from anyone but the configured owner are stored without content and rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xboct.scheduling.state import TELEGRAM_OFFSET, get_state, set_state
from xboct.storage.models import TelegramInbox
from xboct.telegram.transport import Transport

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def describe(update: dict[str, Any]) -> tuple[str, int | None]:
    if "message" in update:
        return "message", (update["message"].get("from") or {}).get("id")
    if "callback_query" in update:
        return "callback", (update["callback_query"].get("from") or {}).get("id")
    return "other", None


def is_owner(update: dict[str, Any], owner_id: int) -> bool:
    kind, from_id = describe(update)
    if from_id != owner_id:
        return False
    if kind == "message":
        chat = update["message"].get("chat") or {}
        return chat.get("type") == "private" and chat.get("id") == owner_id
    return kind == "callback"


async def poll_once(
    sessions: Callable[[], AsyncSession],
    transport: Transport,
    now: Callable[[], datetime],
    owner_id: int,
    *,
    poll_seconds: int = 0,
    stop: asyncio.Event | None = None,
) -> int:
    """Fetch updates and store them before acknowledging. With `stop`, a stop request
    interrupts only the network wait; storing is never cut off halfway."""
    async with sessions() as session:
        state = await get_state(session, TELEGRAM_OFFSET)
    offset = state["offset"] if state else None
    fetch = transport.get_updates(offset, poll_seconds)
    updates = await fetch if stop is None else await until_stopped(fetch, stop)
    if not updates:
        return 0
    stored = 0
    async with sessions() as session, session.begin():
        at = now()
        for update in updates:
            update_id = int(update["update_id"])
            if await session.get(TelegramInbox, update_id) is not None:
                continue
            kind, from_id = describe(update)
            owner = is_owner(update, owner_id)
            session.add(
                TelegramInbox(
                    update_id=update_id,
                    kind=kind,
                    from_id=from_id,
                    payload=update if owner else {"redacted": True},
                    received_at=at,
                    status="received"
                    if owner and kind != "other"
                    else ("ignored" if owner else "rejected"),
                )
            )
            stored += 1
        last = max(int(u["update_id"]) for u in updates)
        await set_state(session, TELEGRAM_OFFSET, {"offset": last + 1}, at)
    return stored


async def until_stopped(
    awaitable: Awaitable[list[dict[str, Any]]], stop: asyncio.Event
) -> list[dict[str, Any]] | None:
    """Await a network call unless stop is set first (then cancel it, return None)."""
    work = asyncio.ensure_future(awaitable)
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
    if work.done():
        return work.result()
    work.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await work
    return None


Handler = Callable[[TelegramInbox], Awaitable[dict[str, Any]]]


async def process_pending(
    sessions: Callable[[], AsyncSession],
    handler: Handler,
    now: Callable[[], datetime],
    stop: asyncio.Event | None = None,
) -> int:
    """Handle stored updates in order. Once `stop` is set no further update is started:
    the one in progress finishes, the rest stays `received` for the next start."""
    async with sessions() as session:
        ids = list(
            await session.scalars(
                select(TelegramInbox.update_id)
                .where(TelegramInbox.status.in_(("received", "processing")))
                .order_by(TelegramInbox.update_id)
            )
        )
    done = 0
    for update_id in ids:
        if stop is not None and stop.is_set():
            break
        async with sessions() as session, session.begin():
            await session.connection()  # BEGIN IMMEDIATE now, not at the first query below
            if stop is not None and stop.is_set():
                break  # the same check inside the transaction that would start the update
            row = await session.get(TelegramInbox, update_id)
            assert row is not None
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                row.processed_at = now()
                continue
            row.status = "processing"
            row.attempts += 1
        try:
            result = await handler(row)
        except Exception:
            log.exception("inbox update failed", extra={"update_id": update_id})
            continue  # stays `processing`; retried next tick, bounded by MAX_ATTEMPTS
        async with sessions() as session, session.begin():
            row = await session.get(TelegramInbox, update_id)
            assert row is not None
            row.status = "done"
            row.result = result
            row.processed_at = now()
        done += 1
    return done


def message_time(update: dict[str, Any], fallback: datetime) -> datetime:
    stamp = (update.get("message") or {}).get("date")
    if isinstance(stamp, int):
        return datetime.fromtimestamp(stamp, UTC)
    if isinstance(stamp, str):
        return datetime.fromisoformat(stamp).astimezone(UTC)
    return fallback
