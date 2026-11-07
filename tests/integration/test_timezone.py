from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from tests.conftest import Harness
from xboct.application import commands as c
from xboct.application.service import Application
from xboct.domain.clock import FixedClock
from xboct.storage.models import AppSettings, NotificationJob, Occurrence, Task

BERLIN = ZoneInfo("Europe/Berlin")
MOSCOW = ZoneInfo("Europe/Moscow")
KRSK = ZoneInfo("Asia/Krasnoyarsk")


@pytest.fixture
async def berlin(db_url, provider) -> AsyncIterator[Harness]:
    clock = FixedClock(datetime(2026, 3, 20, 12, 0, tzinfo=BERLIN))
    app = await Application.open(db_url, clock, default_timezone="Europe/Berlin")
    harness = Harness(app, clock, provider)
    yield harness
    await harness.app.close()


async def test_spring_gap_gives_one_reminder_at_first_valid_instant(berlin):
    # A31: 29 March 02:30 does not exist in Berlin.
    result = await berlin.ok(
        c.CreateTask(title="Ночной деплой", due_date=date(2026, 3, 29), due_time=time(2, 30))
    )
    task = await berlin.get(Task, result.data["task_id"])
    assert task.due_at == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST
    due = [j for j in await berlin.pending_jobs(task.id) if j.kind == "due"]
    assert len(due) == 1


async def test_autumn_repeat_gives_one_reminder_per_day(berlin):
    # A31: daily 02:30 across 25 Oct: the repeated hour yields one job (first occurrence).
    berlin.clock.set(datetime(2026, 10, 20, 12, 0, tzinfo=BERLIN))
    result = await berlin.ok(
        c.CreateTask(
            title="Бэкап",
            recurrence=c.RecurrenceSpec(
                frequency="daily", anchor_date=date(2026, 10, 24), due_time=time(2, 30)
            ),
        )
    )
    task_id = result.data["task_id"]
    occ = (
        await berlin.all(
            Occurrence, Occurrence.owner_id == task_id, Occurrence.nominal_due == date(2026, 10, 25)
        )
    )[0]
    due = [
        j
        for j in await berlin.pending_jobs(task_id)
        if j.kind == "due" and j.occurrence_id == occ.id
    ]
    assert [j.scheduled_at for j in due] == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]
    local_dates = sorted(
        j.scheduled_at.astimezone(BERLIN).date()
        for j in await berlin.pending_jobs(task_id)
        if j.kind == "due"
    )
    assert len(local_dates) == len(set(local_dates))


async def test_timezone_change_replans_future_and_keeps_history(x):
    # A31: date-only reminders follow the new local 10:00; exact instants and sent
    # history stay as they were.
    date_only = await x.ok(c.CreateTask(title="Письмо", due_date=date(2026, 10, 15)))
    exact = await x.ok(
        c.CreateTask(title="Созвон", due_date=date(2026, 10, 16), due_time=time(15, 0))
    )
    async with x.app.sessions() as session, session.begin():
        sent = await session.scalar(
            select(NotificationJob.id).where(
                NotificationJob.subject_id == date_only.data["task_id"],
                NotificationJob.kind == "overdue",
            )
        )
        await session.execute(
            update(NotificationJob).where(NotificationJob.id == sent).values(status="sent")
        )

    settings = await x.get(AppSettings, 1)
    result = await x.ok(
        c.ChangeTimezone(timezone="Europe/Moscow", expected_version=settings.version)
    )
    assert "Europe/Moscow" in result.message

    due = [j for j in await x.pending_jobs(date_only.data["task_id"]) if j.kind == "due"]
    assert [j.scheduled_at.astimezone(MOSCOW) for j in due] == [
        datetime(2026, 10, 15, 10, 0, tzinfo=MOSCOW)
    ]
    exact_task = await x.get(Task, exact.data["task_id"])
    assert exact_task.due_at == datetime(2026, 10, 16, 15, 0, tzinfo=KRSK)
    exact_due = [j for j in await x.pending_jobs(exact_task.id) if j.kind == "due"]
    assert [j.scheduled_at for j in exact_due] == [exact_task.due_at]
    still_sent = await x.get(NotificationJob, sent)
    assert still_sent.status == "sent"
    assert (await x.get(Task, date_only.data["task_id"])).due_date == date(2026, 10, 15)


async def test_unknown_timezone_rejected(x):
    result = await x.run(c.ChangeTimezone(timezone="Mars/Olympus", expected_version=1))
    assert result.code == "bad_timezone"
