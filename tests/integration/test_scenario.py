"""IMPLEMENTATION_PLAN stage 1 scenario: complete -> restart -> next dates / forecast."""

from datetime import date, timedelta

from tests.conftest import at, local
from xboct.application import commands as c
from xboct.application.queries import agenda, balance_forecasts, payments
from xboct.storage.models import Occurrence, Task


async def test_complete_restart_next_dates_and_forecast(x):
    task = await x.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)))
    vps = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            amount_minor=50_000,
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    hosting = await x.ok(
        c.CreateObligation(title="Хостинг", kind="coverage", coverage_until=date(2026, 11, 1))
    )
    hetzner = await x.ok(
        c.CreateObligation(
            title="Hetzner",
            kind="balance",
            balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=4_000),
        )
    )
    await x.ok(c.RecordTopup(obligation_id=hetzner.data["obligation_id"], amount_minor=120_000))

    october = (await x.all(Occurrence, Occurrence.period_key == "M:2026-10"))[0]
    await x.ok(
        c.MarkPayment(
            obligation_id=vps.data["obligation_id"],
            occurrences=[c.OccurrenceRef(id=october.id, expected_version=1)],
            amount_minor=50_000,
        )
    )
    await x.ok(c.CompleteTask(task_id=task.data["task_id"], expected_version=1))
    await x.ok(
        c.ExtendCoverage(
            obligation_id=hosting.data["obligation_id"], expected_version=1, unit="month", count=3
        )
    )

    await x.restart()
    x.clock.advance(days=10)  # 18 October

    assert (await x.get(Task, task.data["task_id"])).status == "completed"
    views = {v.title: v for v in await x.app.read(payments)}
    assert views["VPS"].next_due == date(2026, 11, 15)
    assert views["VPS"].outstanding == []
    assert views["VPS"].last_payment_minor == 50_000
    assert views["Хостинг"].coverage_until == date(2027, 2, 1)
    [(_, fc)] = await x.app.read(balance_forecasts)
    assert fc.days_left_floor == 20
    assert fc.estimate_minor == 80_000
    assert local(fc.depletion_at) == at(2026, 11, 7, 10)

    view = await x.app.read(agenda)
    assert view.overdue == []
    assert view.due_today == []

    jobs = await x.app.jobs(status="pending")
    by_subject = {}
    for job in jobs:
        by_subject.setdefault(job.subject_id, []).append((job.kind, local(job.scheduled_at)))
    assert by_subject[hetzner.data["obligation_id"]] == [("balance_low", at(2026, 11, 2, 10))]
    assert ("due", at(2026, 11, 15, 10)) in by_subject[vps.data["obligation_id"]]
    assert by_subject[hosting.data["obligation_id"]][0] == ("advance", at(2027, 1, 29, 10))
    assert task.data["task_id"] not in by_subject

    # Life continues after restart: another topup moves the warning.
    result = await x.ok(
        c.RecordTopup(obligation_id=hetzner.data["obligation_id"], amount_minor=40_000)
    )
    assert result.data["days_left"] == 30
    jobs = await x.pending_jobs(hetzner.data["obligation_id"])
    assert [local(j.scheduled_at) for j in jobs] == [at(2026, 11, 12, 10)]
    assert jobs[0].scheduled_at - x.clock.now() == timedelta(days=25)


async def test_refresh_schedule_extends_horizon_as_time_passes(x):
    result = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    ob_id = result.data["obligation_id"]
    first = {j.period_key for j in await x.pending_jobs(ob_id)}
    assert first == {"M:2026-10", "M:2026-11"}  # job horizon is about two months

    x.clock.set(at(2027, 9, 20))
    refreshed = await x.app.refresh_schedule(command_id="system:refresh:1")
    assert refreshed.status == "applied"
    assert refreshed.data["occurrences_created"] > 0
    keys = {o.period_key for o in await x.all(Occurrence, Occurrence.owner_id == ob_id)}
    assert "M:2028-09" in keys  # materialized at least 12 months ahead again
    planned = {j.period_key for j in await x.pending_jobs(ob_id)}
    assert {"M:2027-10", "M:2027-11"} <= planned
    # Past unpaid periods stay open (no silent closing). Missed jobs from the old plan
    # stay pending for the stage 2 catch-up digest; refresh never creates past instants.
    assert "M:2026-10" in keys
    created = [j for j in await x.pending_jobs(ob_id) if j.command_id == "system:refresh:1"]
    assert created
    assert all(j.scheduled_at >= x.clock.now() for j in created)

    again = await x.app.refresh_schedule(command_id="system:refresh:2")
    assert again.data["occurrences_created"] == 0
