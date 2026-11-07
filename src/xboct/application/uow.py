"""Unit of work for one command transaction: loading, version checks, audit, replans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from xboct.application.context import CommandContext
from xboct.domain.errors import NotFound, VersionConflict
from xboct.domain.timeutil import get_zone, local_date
from xboct.storage.models import (
    AppSettings,
    AuditEvent,
    BalanceEvent,
    Base,
    Obligation,
    Occurrence,
    Payment,
    Task,
)

T = TypeVar("T", bound=Base)

ENTITY_TYPES: dict[type[Base], str] = {
    Task: "task",
    Obligation: "obligation",
    Occurrence: "occurrence",
    Payment: "payment",
    BalanceEvent: "balance_event",
    AppSettings: "settings",
}
MODELS_BY_TYPE: dict[str, type[Base]] = {v: k for k, v in ENTITY_TYPES.items()}

# Fields an undo may restore. Bookkeeping (schedule_revision, updated_at) is excluded.
RESTORABLE: dict[str, tuple[str, ...]] = {
    "task": (
        "title",
        "note",
        "url",
        "next_action",
        "due_date",
        "due_at",
        "status",
        "completed_at",
        "cancelled_at",
    ),
    "obligation": (
        "title",
        "aliases",
        "amount_minor",
        "note",
        "active",
        "coverage_until",
        "archived_at",
    ),
    "occurrence": ("effective_due", "status", "resolved_at"),
    "payment": ("reverted_at", "reverted_by"),
    "balance_event": ("reverted_at", "reverted_by"),
    "settings": ("timezone",),
}

ENTITY_LABELS = {
    "task": "дело",
    "obligation": "обязательство",
    "occurrence": "период",
    "payment": "платёж",
    "balance_event": "запись баланса",
}


def to_json(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()  # same text before and after a DB round trip
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return value


def snapshot(entity: Base) -> dict[str, Any]:
    mapper = inspect(type(entity))
    return {col.key: to_json(getattr(entity, col.key)) for col in mapper.column_attrs}


def from_json(model: type[Base], field: str, value: Any) -> Any:
    if value is None:
        return None
    column = inspect(model).columns[field]
    python_type = column.type.python_type
    if python_type is datetime:
        return datetime.fromisoformat(value)
    if python_type is date:
        return date.fromisoformat(value)
    if python_type is time:
        return time.fromisoformat(value)
    return value


def restorable_view(entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {key: data.get(key) for key in RESTORABLE[entity_type]}


class Uow:
    def __init__(
        self,
        session: AsyncSession,
        ctx: CommandContext,
        now: datetime,
        settings: AppSettings,
        audit_command_id: str | None = None,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.now = now
        self.settings = settings
        self.audit_command_id = audit_command_id or ctx.command_id
        self.replan: set[tuple[str, str]] = set()
        # Run by Application.run when the work ends in a clarification (the work itself is rolled
        # back): lets a failed attempt leave the stored state honest.
        self.on_clarification: list[Callable[[], Awaitable[None]]] = []

    @property
    def tz(self) -> ZoneInfo:
        return get_zone(self.settings.timezone)

    @property
    def today(self) -> date:
        """Local date of the request (relative dates are resolved against it)."""
        return local_date(self.ctx.received_at, self.tz)

    @property
    def today_now(self) -> date:
        """Local date of the processing instant (for overdue/horizon decisions)."""
        return local_date(self.now, self.tz)

    async def get(self, model: type[T], entity_id: str) -> T:
        entity = await self.session.get(model, entity_id)
        if entity is None:
            label = ENTITY_LABELS.get(ENTITY_TYPES.get(model, ""), "запись")
            raise NotFound(f"Не нашёл {label} с id {entity_id}", entity_id=entity_id)
        return entity

    async def get_versioned(self, model: type[T], entity_id: str, expected: int | None) -> T:
        entity = await self.get(model, entity_id)
        if expected is not None and entity.version != expected:  # type: ignore[attr-defined]
            raise VersionConflict(
                "Запись изменилась после того, как ты её видел. Обнови данные и повтори.",
                entity_id=entity_id,
                expected_version=expected,
                actual_version=entity.version,  # type: ignore[attr-defined]
            )
        return entity

    def touch(self, entity: Base) -> None:
        entity.version += 1  # type: ignore[attr-defined]
        if hasattr(entity, "updated_at"):
            entity.updated_at = self.now  # type: ignore[attr-defined]

    def mark_replan(self, subject_type: str, subject_id: str) -> None:
        self.replan.add((subject_type, subject_id))

    async def audit(
        self,
        entity: Base,
        action: str,
        before: dict[str, Any] | None,
        *,
        parent_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        await self.session.flush()
        entity_type = ENTITY_TYPES[type(entity)]
        entity_id = str(entity.id)  # type: ignore[attr-defined]
        self.session.add(
            AuditEvent(
                command_id=self.audit_command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                parent_id=parent_id or entity_id,
                action=action,
                before=before,
                after=snapshot(entity),
                reason=reason,
                created_at=self.now,
            )
        )

    async def open_occurrences(self, owner_type: str, owner_id: str) -> list[Occurrence]:
        rows = await self.session.scalars(
            select(Occurrence)
            .where(
                Occurrence.owner_type == owner_type,
                Occurrence.owner_id == owner_id,
                Occurrence.status == "open",
            )
            .order_by(Occurrence.effective_due, Occurrence.nominal_due)
        )
        return list(rows)
