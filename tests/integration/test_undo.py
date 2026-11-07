from datetime import date

from tests.conftest import at
from xboct.application import commands as c
from xboct.storage.models import AuditEvent, BalanceEvent, Obligation, Occurrence, Payment, Task


async def calendar(x) -> tuple[Obligation, Occurrence]:
    result = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    ob = await x.get(Obligation, result.data["obligation_id"])
    occ = (
        await x.all(Occurrence, Occurrence.owner_id == ob.id, Occurrence.period_key == "M:2026-10")
    )[0]
    return ob, occ


async def test_undo_payment_is_compensation_not_deletion(x):
    # A25
    ob, occ = await calendar(x)
    await x.ok(
        c.MarkPayment(
            obligation_id=ob.id, occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)]
        ),
        command_id="pay-1",
    )
    assert not [j for j in await x.pending_jobs(ob.id) if j.occurrence_id == occ.id]

    result = await x.ok(c.UndoCommand(), command_id="undo-1")
    assert result.data["undone_command_id"] == "pay-1"
    occ = await x.get(Occurrence, occ.id)
    assert occ.status == "open"
    assert occ.version == 3  # open -> paid -> open, each a recorded change
    payment = (await x.all(Payment))[0]
    assert payment.reverted_by == "undo-1"
    assert payment.reverted_at is not None
    actions = [a.action for a in await x.all(AuditEvent, AuditEvent.command_id == "undo-1")]
    assert sorted(actions) == ["undo", "undo"]
    # Reminders for the reopened period are planned again.
    kinds = {j.kind for j in await x.pending_jobs(ob.id) if j.occurrence_id == occ.id}
    assert kinds == {"advance", "due", "overdue"}


async def test_undo_topup_restores_forecast(x):
    result = await x.ok(
        c.CreateObligation(
            title="Hetzner",
            kind="balance",
            balance=c.BalanceStart(checkpoint_minor=40_000, rate_minor_per_day=4_000),
        )
    )
    ob_id = result.data["obligation_id"]
    before = await x.pending_jobs(ob_id)
    await x.ok(c.RecordTopup(obligation_id=ob_id, amount_minor=120_000))
    assert [j.scheduled_at for j in await x.pending_jobs(ob_id)] != [j.scheduled_at for j in before]
    await x.ok(c.UndoCommand())
    events = await x.all(BalanceEvent, BalanceEvent.obligation_id == ob_id)
    assert len(events) == 3  # correction, rate, topup — the topup is kept but reverted
    topup = next(e for e in events if e.kind == "topup")
    assert topup.reverted_at is not None
    after = await x.pending_jobs(ob_id)
    assert [j.scheduled_at for j in after] == [j.scheduled_at for j in before]


async def test_dependent_later_change_blocks_undo(x):
    result = await x.ok(
        c.CreateObligation(title="Хостинг", kind="coverage", coverage_until=date(2026, 10, 15))
    )
    ob_id = result.data["obligation_id"]
    await x.ok(
        c.ExtendCoverage(obligation_id=ob_id, expected_version=1, unit="month", count=1),
        command_id="ext-1",
    )
    await x.ok(
        c.ExtendCoverage(obligation_id=ob_id, expected_version=2, unit="month", count=1),
        command_id="ext-2",
    )
    blocked = await x.run(c.UndoCommand(target_command_id="ext-1"))
    assert blocked.status == "rejected"
    assert blocked.code == "undo_conflict"
    assert "поправь" in blocked.message
    assert (await x.get(Obligation, ob_id)).coverage_until == date(2026, 12, 15)

    await x.ok(c.UndoCommand())  # last -> ext-2
    assert (await x.get(Obligation, ob_id)).coverage_until == date(2026, 11, 15)
    await x.ok(c.UndoCommand(target_command_id="ext-1"))  # dependency gone now
    ob = await x.get(Obligation, ob_id)
    assert ob.coverage_until == date(2026, 10, 15)
    assert all(p.reverted_at is not None for p in await x.all(Payment))


async def test_undo_create_obligation_blocked_by_later_payment(x):
    ob, occ = await calendar(x)
    await x.ok(
        c.MarkPayment(
            obligation_id=ob.id, occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)]
        )
    )
    receipts = [a.command_id for a in await x.all(AuditEvent, AuditEvent.entity_id == ob.id)]
    blocked = await x.run(c.UndoCommand(target_command_id=receipts[0]))
    assert blocked.code == "undo_conflict"
    assert (await x.get(Obligation, ob.id)).active


async def test_undo_twice_and_undo_of_undo(x):
    result = await x.ok(
        c.CreateTask(title="Ответить", due_date=date(2026, 10, 15)), command_id="create-1"
    )
    await x.ok(c.UndoCommand(target_command_id="create-1"), command_id="undo-1")
    task = await x.get(Task, result.data["task_id"])
    assert task.status == "cancelled"
    assert await x.pending_jobs(task.id) == []
    again = await x.run(c.UndoCommand(target_command_id="create-1"))
    assert again.code == "already_undone"
    meta = await x.run(c.UndoCommand(target_command_id="undo-1"))
    assert meta.code == "not_undoable"


async def test_undo_last_skips_non_undoable_snooze(x):
    result = await x.ok(c.CreateTask(title="Ответить", due_date=date(2026, 10, 15)))
    task_id = result.data["task_id"]
    await x.ok(
        c.UpdateTask(task_id=task_id, expected_version=1, next_action="отправить"), command_id="upd"
    )
    await x.ok(
        c.SnoozeNotification(
            subject_type="task", subject_id=task_id, until=x.clock.now().replace(hour=12)
        )
    )
    undo = await x.ok(c.UndoCommand())
    assert undo.data["undone_command_id"] == "upd"
    task = await x.get(Task, task_id)
    assert task.next_action is None
    assert task.status == "open"


async def test_undo_conflict_on_same_entity_later_edit(x):
    result = await x.ok(c.CreateTask(title="Ответить", due_date=date(2026, 10, 15)))
    task_id = result.data["task_id"]
    await x.ok(c.UpdateTask(task_id=task_id, expected_version=1, note="a"), command_id="a")
    await x.ok(
        c.UpdateTask(task_id=task_id, expected_version=2, title="Ответить Юле"), command_id="b"
    )
    blocked = await x.run(c.UndoCommand(target_command_id="a"))
    assert blocked.code == "undo_conflict"
    assert (await x.get(Task, task_id)).note == "a"


async def test_undo_payment_with_local_paid_at(x):
    # Audit snapshots must compare equal after a DB round trip (aware time -> UTC).
    ob, occ = await calendar(x)
    await x.ok(
        c.MarkPayment(
            obligation_id=ob.id,
            occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)],
            paid_at=at(2026, 10, 7, 18),
        )
    )
    await x.restart()
    await x.ok(c.UndoCommand())
    assert (await x.get(Occurrence, occ.id)).status == "open"


async def test_rejection_text_has_single_period(x):
    result = await x.run(c.UpdateTask(task_id="missing", expected_version=1, note="x"))
    assert result.code == "not_found"
    assert ".." not in result.message
