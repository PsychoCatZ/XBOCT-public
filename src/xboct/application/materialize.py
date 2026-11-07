"""Occurrences of recurring tasks and calendar obligations.

At least 12 months ahead are materialized; requests beyond the horizon extend it.
Overdue occurrences are never deleted. A rule change touches only future open periods.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from xboct.application.uow import Uow
from xboct.domain.recurrence import RecurrenceRule
from xboct.domain.timeutil import add_months
from xboct.storage.models import Occurrence, Recurrence

MATERIALIZE_MONTHS = 12


def rule_of(recurrence: Recurrence) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=recurrence.frequency,  # type: ignore[arg-type]
        anchor_date=recurrence.anchor_date,
        interval=recurrence.interval,
        weekdays=tuple(recurrence.weekdays or ()),
        monthday=recurrence.monthday,
    )


def horizon_for(today: date) -> date:
    return add_months(today, MATERIALIZE_MONTHS)


async def ensure_materialized(uow: Uow, recurrence: Recurrence, until: date) -> int:
    """Create missing occurrences up to `until` (inclusive). Returns how many were created."""
    if recurrence.materialized_until is not None and recurrence.materialized_until >= until:
        return 0
    rule = rule_of(recurrence)
    start = (
        recurrence.materialized_until + timedelta(days=1)
        if recurrence.materialized_until is not None
        else rule.anchor_date
    )
    existing = set(
        await uow.session.scalars(
            select(Occurrence.period_key).where(
                Occurrence.owner_type == recurrence.owner_type,
                Occurrence.owner_id == recurrence.owner_id,
            )
        )
    )
    created = 0
    for nominal in rule.dates_between(start, until):
        key = rule.period_key(nominal)
        if key in existing:
            continue
        uow.session.add(
            Occurrence(
                owner_type=recurrence.owner_type,
                owner_id=recurrence.owner_id,
                recurrence_id=recurrence.id,
                period_key=key,
                nominal_due=nominal,
                effective_due=nominal,
                status="open",
                created_at=uow.now,
                updated_at=uow.now,
            )
        )
        existing.add(key)
        created += 1
    recurrence.materialized_until = until
    await uow.session.flush()
    return created


async def apply_rule_change(uow: Uow, recurrence: Recurrence, new_rule: RecurrenceRule) -> None:
    """Re-plan future open periods under a new rule; paid/done history is untouched."""
    today = uow.today
    horizon = max(horizon_for(today), recurrence.materialized_until or today)
    rows = list(
        await uow.session.scalars(
            select(Occurrence).where(
                Occurrence.owner_type == recurrence.owner_type,
                Occurrence.owner_id == recurrence.owner_id,
            )
        )
    )
    by_key = {occ.period_key: occ for occ in rows}
    wanted: dict[str, date] = {
        new_rule.period_key(d): d for d in new_rule.dates_between(today, horizon)
    }

    for occ in rows:
        is_future_open = occ.status == "open" and occ.nominal_due >= today
        if is_future_open and occ.period_key not in wanted:
            occ.status = "superseded"
            occ.resolved_at = uow.now
            uow.touch(occ)

    for key, nominal in wanted.items():
        occ = by_key.get(key)
        if occ is None:
            uow.session.add(
                Occurrence(
                    owner_type=recurrence.owner_type,
                    owner_id=recurrence.owner_id,
                    recurrence_id=recurrence.id,
                    period_key=key,
                    nominal_due=nominal,
                    effective_due=nominal,
                    status="open",
                    created_at=uow.now,
                    updated_at=uow.now,
                )
            )
            continue
        # paid/done/skipped/cancelled periods and overdue open ones are history: keep them.
        revivable = occ.status == "superseded" or (
            occ.status == "open" and occ.nominal_due >= today
        )
        if revivable and (occ.nominal_due, occ.effective_due, occ.status) != (
            nominal,
            nominal,
            "open",
        ):
            occ.nominal_due = nominal
            occ.effective_due = nominal
            occ.status = "open"
            occ.resolved_at = None
            uow.touch(occ)

    recurrence.frequency = new_rule.frequency
    recurrence.anchor_date = new_rule.anchor_date
    recurrence.interval = new_rule.interval
    recurrence.weekdays = list(new_rule.weekdays)
    recurrence.monthday = new_rule.monthday
    recurrence.materialized_until = horizon
    uow.touch(recurrence)
    await uow.session.flush()
