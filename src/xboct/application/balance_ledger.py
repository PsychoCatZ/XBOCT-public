"""Loading the balance ledger of an obligation and building its forecast."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from xboct.application.uow import Uow
from xboct.domain.balance import BalanceForecast, LedgerEvent, ReplayState, forecast, replay
from xboct.storage.models import BalanceEvent, Obligation


def to_ledger(event: BalanceEvent) -> LedgerEvent:
    return LedgerEvent(
        id=event.id,
        at=event.effective_at,
        kind=event.kind,  # type: ignore[arg-type]
        seq=event.seq,
        amount_minor=event.amount_minor,
        rate_minor_per_day=event.rate_minor_per_day,
        assumed=event.assumed,
    )


async def active_events(uow: Uow, obligation_id: str) -> list[LedgerEvent]:
    rows = await uow.session.scalars(
        select(BalanceEvent)
        .where(BalanceEvent.obligation_id == obligation_id, BalanceEvent.reverted_at.is_(None))
        .order_by(BalanceEvent.effective_at, BalanceEvent.seq)
    )
    return [to_ledger(e) for e in rows]


async def load_forecast(uow: Uow, ob: Obligation, at: datetime | None = None) -> BalanceForecast:
    events = await active_events(uow, ob.id)
    return forecast(events, at or uow.now, uow.settings.balance_threshold_days)


async def estimate_before(uow: Uow, obligation_id: str, at: datetime) -> ReplayState:
    """Replay state just at `at`, with events at exactly `at` included."""
    return replay(await active_events(uow, obligation_id), at)


async def add_event(
    uow: Uow,
    ob: Obligation,
    kind: str,
    effective_at: datetime,
    *,
    amount_minor: int | None = None,
    rate_minor_per_day: int | None = None,
    assumed: bool = False,
) -> BalanceEvent:
    next_seq = (
        await uow.session.scalar(
            select(func.coalesce(func.max(BalanceEvent.seq), 0)).where(
                BalanceEvent.obligation_id == ob.id
            )
        )
        or 0
    ) + 1
    event = BalanceEvent(
        obligation_id=ob.id,
        seq=next_seq,
        effective_at=effective_at,
        kind=kind,
        amount_minor=amount_minor,
        rate_minor_per_day=rate_minor_per_day,
        assumed=assumed,
        command_id=uow.audit_command_id,
        created_at=uow.now,
    )
    uow.session.add(event)
    await uow.audit(event, "create", None, parent_id=ob.id)
    uow.mark_replan("obligation", ob.id)
    return event
