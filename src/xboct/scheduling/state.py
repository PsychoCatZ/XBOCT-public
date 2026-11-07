"""Durable runtime facts in SQLite (runtime_state table)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from xboct.storage.models import RuntimeState

TELEGRAM_OFFSET = "telegram_offset"
LAST_REFRESH = "last_refresh"
LAST_TICK = "last_tick"
TELEGRAM_HEALTH = "telegram_health"
DIGEST_SINCE = "digest_since"
LAST_BACKUP = "last_backup"


async def get_state(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(RuntimeState, key)
    return dict(row.value) if row is not None else None


async def set_state(session: AsyncSession, key: str, value: dict[str, Any], now: datetime) -> None:
    row = await session.get(RuntimeState, key)
    if row is None:
        session.add(RuntimeState(key=key, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
