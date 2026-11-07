"""Time-driven upkeep: extend the materialized horizon and plan jobs that came into range.

Stage 2's scheduler calls Application.refresh_schedule() periodically (e.g. daily and at
startup). It is idempotent: re-running it on the same state changes nothing.
"""

from __future__ import annotations

from sqlalchemy import select

from xboct.application.context import Outcome
from xboct.application.handlers import apply_replans
from xboct.application.materialize import ensure_materialized, horizon_for
from xboct.application.uow import Uow
from xboct.storage.models import Obligation, Recurrence, Task


async def refresh_schedule(uow: Uow) -> Outcome:
    horizon = horizon_for(uow.today_now)
    open_tasks = set(await uow.session.scalars(select(Task.id).where(Task.status == "open")))
    active_obligations = set(
        await uow.session.scalars(select(Obligation.id).where(Obligation.active))
    )
    created = 0
    for recurrence in await uow.session.scalars(select(Recurrence)):
        owners = open_tasks if recurrence.owner_type == "task" else active_obligations
        if recurrence.owner_id in owners:
            created += await ensure_materialized(uow, recurrence, horizon)
    for task_id in open_tasks:
        uow.mark_replan("task", task_id)
    for obligation_id in active_obligations:
        uow.mark_replan("obligation", obligation_id)
    await apply_replans(uow)
    return Outcome(
        f"Расписание обновлено до {horizon.isoformat()}: новых периодов {created}.",
        {"horizon": horizon.isoformat(), "occurrences_created": created},
        undoable=False,
    )
