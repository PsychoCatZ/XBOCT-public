from datetime import date, datetime, time

import pytest
from pydantic import ValidationError

from tests.conftest import at, local
from xboct.application import commands as c
from xboct.storage.models import AuditEvent, NotificationJob, Occurrence, Task


async def create(x, **kw) -> Task:
    result = await x.ok(c.CreateTask(**kw))
    return await x.get(Task, result.data["task_id"])


async def test_date_only_task_gets_default_notice_and_one_overdue_followup(x):
    # A01/A04: explicit date stored as local date; default 10:00 notice is visible.
    result = await x.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)))
    assert "15 октября" in result.message
    assert "10:00 (время по умолчанию" in result.message
    task = await x.get(Task, result.data["task_id"])
    assert task.due_date == date(2026, 10, 15)
    assert task.due_at is None
    jobs = await x.pending_jobs(task.id)
    assert [(j.kind, local(j.scheduled_at)) for j in jobs] == [
        ("due", at(2026, 10, 15, 10)),
        # 24h after the end of the due day is 00:00 -> moved out of quiet hours.
        ("overdue", at(2026, 10, 17, 9)),
    ]
    assert len({j.dedup_key for j in jobs}) == 2


async def test_exact_time_task_keeps_instant(x):
    task = await create(x, title="Созвон", due_date=date(2026, 10, 9), due_time=time(14, 30))
    assert task.due_date is None
    assert task.due_at == at(2026, 10, 9, 14, 30)
    due = [j for j in await x.pending_jobs(task.id) if j.kind == "due"]
    assert due[0].scheduled_at == at(2026, 10, 9, 14, 30)
    assert due[0].explicit


async def test_undated_task_has_no_invented_due(x):
    result = await x.ok(c.CreateTask(title="Разобрать почту"))
    assert "не придумываю" in result.message
    task = await x.get(Task, result.data["task_id"])
    assert task.due_date is None
    assert task.due_at is None
    assert await x.pending_jobs(task.id) == []


async def test_ready_answer_is_not_a_sent_answer(x):
    # A02: next_action changes, the task stays open, deadline unchanged.
    task = await create(x, title="Ответить Юле", due_date=date(2026, 10, 15))
    result = await x.ok(
        c.UpdateTask(
            task_id=task.id, expected_version=task.version, next_action="Отправить ответ вечером"
        )
    )
    assert "Не закрыто" in result.message
    task = await x.get(Task, task.id)
    assert task.status == "open"
    assert task.next_action == "Отправить ответ вечером"
    assert task.due_date == date(2026, 10, 15)
    assert task.version == 2


async def test_sent_answer_completes_and_cancels_jobs(x):
    # A03
    task = await create(x, title="Ответить Юле", due_date=date(2026, 10, 15))
    await x.ok(c.CompleteTask(task_id=task.id, expected_version=task.version))
    task = await x.get(Task, task.id)
    assert task.status == "completed"
    assert task.completed_at == x.clock.now()
    assert await x.pending_jobs(task.id) == []
    cancelled = await x.all(NotificationJob, NotificationJob.subject_id == task.id)
    assert {j.status for j in cancelled} == {"cancelled"}
    assert {j.cancel_reason for j in cancelled} == {"closed"}


async def test_snooze_does_not_move_deadline(x):
    # A05: due unchanged; one new reminder at 19:00; the earlier reminder is replaced.
    x.clock.set(at(2026, 10, 15, 10, 5))
    task = await create(x, title="Ответить Юле", due_date=date(2026, 10, 15), due_time=time(12, 0))
    result = await x.ok(
        c.SnoozeNotification(subject_type="task", subject_id=task.id, until=at(2026, 10, 15, 19))
    )
    assert "Срок не трогаю" in result.message
    after = await x.get(Task, task.id)
    assert after.due_at == task.due_at
    assert after.version == task.version
    pending = await x.pending_jobs(task.id)
    snoozes = [j for j in pending if j.kind == "snooze"]
    assert [local(j.scheduled_at) for j in snoozes] == [at(2026, 10, 15, 19)]
    assert all(j.kind != "due" for j in pending)  # 12:00 reminder replaced
    assert any(j.kind == "overdue" for j in pending)  # later follow-up kept

    # A second snooze replaces the first one instead of adding another.
    await x.ok(
        c.SnoozeNotification(subject_type="task", subject_id=task.id, until=at(2026, 10, 15, 21))
    )
    snoozes = [j for j in await x.pending_jobs(task.id) if j.kind == "snooze"]
    assert [local(j.scheduled_at) for j in snoozes] == [at(2026, 10, 15, 21)]


async def test_snooze_survives_unrelated_edit_and_dies_with_completion(x):
    task = await create(x, title="Ответить Юле", due_date=date(2026, 10, 15))
    await x.ok(
        c.SnoozeNotification(subject_type="task", subject_id=task.id, until=at(2026, 10, 8, 19))
    )
    task = await x.get(Task, task.id)
    await x.ok(c.UpdateTask(task_id=task.id, expected_version=task.version, note="черновик"))
    assert any(j.kind == "snooze" for j in await x.pending_jobs(task.id))
    task = await x.get(Task, task.id)
    await x.ok(c.CompleteTask(task_id=task.id, expected_version=task.version))
    assert await x.pending_jobs(task.id) == []


async def test_snooze_in_the_past_is_rejected(x):
    task = await create(x, title="Ответить", due_date=date(2026, 10, 15))
    result = await x.run(
        c.SnoozeNotification(subject_type="task", subject_id=task.id, until=at(2026, 10, 8, 9))
    )
    assert result.status == "rejected"
    assert result.code == "snooze_in_past"


async def test_due_change_replans_jobs_with_new_revision(x):
    task = await create(x, title="Ответить", due_date=date(2026, 10, 15))
    first = await x.pending_jobs(task.id)
    await x.ok(
        c.UpdateTask(task_id=task.id, expected_version=task.version, due_date=date(2026, 10, 20))
    )
    jobs = await x.all(NotificationJob, NotificationJob.subject_id == task.id)
    old = [j for j in jobs if j.id in {f.id for f in first}]
    assert {j.status for j in old} == {"cancelled"}
    new = [j for j in jobs if j.status == "pending"]
    assert [local(j.scheduled_at) for j in new if j.kind == "due"] == [at(2026, 10, 20, 10)]
    assert all(j.revision == 2 for j in new)
    assert all(j.dedup_key.endswith(":2") for j in new)


async def test_stale_version_is_rejected_without_change(x):
    task = await create(x, title="Ответить", due_date=date(2026, 10, 15))
    await x.ok(c.UpdateTask(task_id=task.id, expected_version=1, note="v2"))
    result = await x.run(c.UpdateTask(task_id=task.id, expected_version=1, note="stale"))
    assert result.status == "rejected"
    assert result.code == "version_conflict"
    assert "Данные не менял" in result.message
    assert (await x.get(Task, task.id)).note == "v2"


async def test_cancel_task_keeps_history(x):
    task = await create(x, title="Ответить", due_date=date(2026, 10, 15))
    await x.ok(c.CancelTask(task_id=task.id, expected_version=task.version))
    task = await x.get(Task, task.id)
    assert task.status == "cancelled"
    audit = await x.all(AuditEvent, AuditEvent.entity_id == task.id)
    assert [a.action for a in audit] == ["create", "update"]
    assert audit[1].before["status"] == "open"
    assert audit[1].after["status"] == "cancelled"


def test_unsafe_url_rejected():
    with pytest.raises(ValidationError):
        c.CreateTask(title="x", url="javascript:alert(1)")
    assert c.CreateTask(title="x", url="https://example.org/a").url == "https://example.org/a"


async def test_recurring_task_completes_current_occurrence_only(x):
    result = await x.ok(
        c.CreateTask(
            title="Полить цветы",
            recurrence=c.RecurrenceSpec(frequency="weekly", anchor_date=date(2026, 10, 8)),
        )
    )
    task = await x.get(Task, result.data["task_id"])
    await x.ok(c.CompleteTask(task_id=task.id, expected_version=task.version))
    occs = await x.all(Occurrence, Occurrence.owner_id == task.id)
    by_date = {o.nominal_due: o.status for o in occs}
    assert by_date[date(2026, 10, 8)] == "done"
    assert by_date[date(2026, 10, 15)] == "open"
    assert (await x.get(Task, task.id)).status == "open"
    # About 12 months are materialized, history is never pruned.
    assert max(by_date) >= date(2027, 10, 1)


async def test_reschedule_one_occurrence_keeps_rule(x):
    result = await x.ok(
        c.CreateTask(
            title="Отчёт",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 20)),
        )
    )
    occs = sorted(
        await x.all(Occurrence, Occurrence.owner_id == result.data["task_id"]),
        key=lambda o: o.nominal_due,
    )
    october, november = occs[0], occs[1]
    await x.ok(
        c.RescheduleOccurrence(
            occurrence_id=october.id, expected_version=october.version, new_due=date(2026, 10, 22)
        )
    )
    october = await x.get(Occurrence, october.id)
    assert (october.nominal_due, october.effective_due) == (date(2026, 10, 20), date(2026, 10, 22))
    assert (await x.get(Occurrence, november.id)).effective_due == date(2026, 11, 20)


async def test_naive_datetime_never_reaches_storage(x):
    with pytest.raises(ValidationError):
        c.SnoozeNotification(subject_type="task", subject_id="t", until=datetime(2026, 10, 8))  # noqa: DTZ001
