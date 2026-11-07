"""Approximate prepaid balance: replay of ledger events, never a stored «true» balance.

estimated(t2) = estimated(t1) - rate_per_day * elapsed_seconds / 86400.
topup adds, correction replaces the estimate, rate_change sets the following rate.
This is a continuous average-spend model, not the provider's billing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

EventKind = Literal["topup", "correction", "rate_change"]
ForecastStatus = Literal["no_baseline", "no_rate", "stopped", "ok", "low", "depleted"]

SECONDS_PER_DAY = Decimal(86400)


@dataclass(frozen=True)
class LedgerEvent:
    id: str
    at: datetime
    kind: EventKind
    seq: int
    amount_minor: int | None = None
    rate_minor_per_day: int | None = None
    assumed: bool = False


@dataclass(frozen=True)
class ReplayState:
    estimate: Decimal | None
    rate: int | None
    baseline: LedgerEvent | None
    funding: LedgerEvent | None
    """Last correction or topup applied on a baseline: identifies the funding epoch."""


@dataclass(frozen=True)
class BalanceForecast:
    as_of: datetime
    status: ForecastStatus
    estimate_minor: Decimal | None
    rate_minor_per_day: int | None
    days_left: Decimal | None
    depletion_at: datetime | None
    threshold_at: datetime | None
    threshold_days: int
    baseline_event_id: str | None
    baseline_at: datetime | None
    baseline_assumed: bool
    funding_event_id: str | None
    funding_at: datetime | None

    @property
    def days_left_floor(self) -> int | None:
        if self.days_left is None:
            return None
        return int(self.days_left.to_integral_value(rounding=ROUND_FLOOR))


def ordered(events: Iterable[LedgerEvent]) -> list[LedgerEvent]:
    return sorted(events, key=lambda e: (e.at, e.seq))


def replay(events: Sequence[LedgerEvent], until: datetime) -> ReplayState:
    """Replay events with at <= until and advance the estimate to `until`."""
    estimate: Decimal | None = None
    rate: int | None = None
    cursor: datetime | None = None
    baseline: LedgerEvent | None = None
    funding: LedgerEvent | None = None

    def advance(to: datetime) -> None:
        nonlocal estimate, cursor
        if estimate is not None and rate and cursor is not None and to > cursor:
            elapsed = Decimal((to - cursor) // timedelta(microseconds=1)) / Decimal(1_000_000)
            estimate -= Decimal(rate) * elapsed / SECONDS_PER_DAY
        cursor = to if cursor is None or to > cursor else cursor

    for event in ordered(events):
        if event.at > until:
            break
        advance(event.at)
        if event.kind == "correction":
            estimate = Decimal(event.amount_minor or 0)
            baseline = event
            funding = event
        elif event.kind == "topup":
            # Without a baseline the starting balance is unknown: do not invent one.
            if estimate is not None:
                estimate += Decimal(event.amount_minor or 0)
                funding = event
        elif event.kind == "rate_change":
            rate = event.rate_minor_per_day
    advance(until)
    return ReplayState(estimate=estimate, rate=rate, baseline=baseline, funding=funding)


def forecast(events: Sequence[LedgerEvent], now: datetime, threshold_days: int) -> BalanceForecast:
    state = replay(events, now)
    depletion_at: datetime | None = None
    threshold_at: datetime | None = None
    days_left: Decimal | None = None

    if state.estimate is None:
        status: ForecastStatus = "no_baseline"
    elif state.rate is None:
        status = "no_rate"
    elif state.rate == 0:
        status = "stopped"
    else:
        days_left = state.estimate / Decimal(state.rate)
        seconds = days_left * SECONDS_PER_DAY
        depletion_at = now + timedelta(microseconds=int(seconds * 1_000_000))
        threshold_at = depletion_at - timedelta(days=threshold_days)
        if state.estimate <= 0:
            status = "depleted"
        elif days_left <= threshold_days:
            status = "low"
        else:
            status = "ok"

    return BalanceForecast(
        as_of=now,
        status=status,
        estimate_minor=state.estimate,
        rate_minor_per_day=state.rate,
        days_left=days_left if days_left is not None and days_left > 0 else None,
        depletion_at=depletion_at,
        threshold_at=threshold_at,
        threshold_days=threshold_days,
        baseline_event_id=state.baseline.id if state.baseline else None,
        baseline_at=state.baseline.at if state.baseline else None,
        baseline_assumed=bool(state.baseline and state.baseline.assumed),
        funding_event_id=state.funding.id if state.funding else None,
        funding_at=state.funding.at if state.funding else None,
    )
