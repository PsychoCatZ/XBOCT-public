from datetime import date

import pytest

from tests.conftest import at
from xboct.application import commands as c
from xboct.application import service as service_module
from xboct.application.context import CommandContext
from xboct.application.queries import agenda
from xboct.storage.models import (
    BalanceEvent,
    CommandReceipt,
    NotificationJob,
    Obligation,
    Occurrence,
    Payment,
    Task,
)


async def balance(x) -> Obligation:
    result = await x.ok(
        c.CreateObligation(
            title="Hetzner",
            kind="balance",
            balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=4_000),
        )
    )
    return await x.get(Obligation, result.data["obligation_id"])


async def test_repeated_update_does_not_double_topup(x):
    # A16: the same Telegram update / web idempotency key twice -> one ledger event.
    ob = await balance(x)
    topup = c.RecordTopup(obligation_id=ob.id, amount_minor=120_000)
    first = await x.run(topup, command_id="tg-update:1001")
    second = await x.run(topup, command_id="tg-update:1001")
    assert first.status == second.status == "applied"
    assert not first.replayed
    assert second.replayed
    assert second.message == first.message
    topups = await x.all(
        BalanceEvent, BalanceEvent.obligation_id == ob.id, BalanceEvent.kind == "topup"
    )
    assert len(topups) == 1
    assert len(await x.pending_jobs(ob.id)) == 1


async def test_repeated_callback_token_pays_once(x):
    # A16: a semantic callback token (entity + version + action) instead of a click id.
    result = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    occ = (
        await x.all(
            Occurrence,
            Occurrence.owner_id == result.data["obligation_id"],
            Occurrence.period_key == "M:2026-10",
        )
    )[0]
    token = f"cb:paid:{occ.id}:v{occ.version}"
    command = c.MarkPayment(
        obligation_id=result.data["obligation_id"],
        occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)],
    )
    for _ in range(3):
        assert (await x.run(command, command_id=token)).status == "applied"
    assert len(await x.all(Payment)) == 1


async def test_same_id_with_other_payload_is_refused(x):
    ob = await balance(x)
    await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=1_000), command_id="web:k1")
    other = await x.run(c.RecordTopup(obligation_id=ob.id, amount_minor=9_999), command_id="web:k1")
    assert other.status == "rejected"
    assert other.code == "idempotency_conflict"
    topups = await x.all(BalanceEvent, BalanceEvent.kind == "topup")
    assert [e.amount_minor for e in topups] == [1_000]


async def test_rejection_is_stored_and_replayed(x):
    result = await x.ok(c.CreateTask(title="Ответить", due_date=date(2026, 10, 15)))
    task_id = result.data["task_id"]
    await x.ok(c.UpdateTask(task_id=task_id, expected_version=1, note="a"))
    stale = c.UpdateTask(task_id=task_id, expected_version=1, note="b")
    first = await x.run(stale, command_id="tg-update:7")
    assert first.code == "version_conflict"
    # Even after the data would allow it, the same update keeps its original answer.
    again = await x.run(stale, command_id="tg-update:7")
    assert again.replayed
    assert again.code == "version_conflict"
    assert (await x.get(Task, task_id)).note == "a"


async def test_crash_after_commit_before_ack_returns_stored_result(x):
    # A17: commit happened, the process died before acknowledging; the retry after
    # restart must not apply the topup again.
    ob = await balance(x)
    command = c.RecordTopup(obligation_id=ob.id, amount_minor=120_000)
    first = await x.run(command, command_id="tg-update:2002")
    await x.restart()
    retry = await x.run(command, command_id="tg-update:2002")
    assert retry.replayed
    assert retry.data == first.data
    assert len(await x.all(BalanceEvent, BalanceEvent.kind == "topup")) == 1
    receipt = await x.get(CommandReceipt, "tg-update:2002")
    assert receipt.status == "applied"


async def test_failure_inside_transaction_leaves_nothing(x, monkeypatch):
    # A crash before commit: no task, no audit, no jobs, no receipt -> a retry applies once.
    calls = {"n": 0}
    original = service_module.apply_replans

    async def exploding(uow):
        calls["n"] += 1
        raise RuntimeError("process died")

    monkeypatch.setattr(service_module, "apply_replans", exploding)
    command = c.CreateTask(title="Ответить", due_date=date(2026, 10, 15))
    with pytest.raises(RuntimeError):
        await x.run(command, command_id="tg-update:3003")
    assert await x.all(Task) == []
    assert await x.all(NotificationJob) == []
    assert await x.all(CommandReceipt, CommandReceipt.command_id == "tg-update:3003") == []

    monkeypatch.setattr(service_module, "apply_replans", original)
    result = await x.run(command, command_id="tg-update:3003")
    assert result.status == "applied"
    assert not result.replayed
    assert len(await x.all(Task)) == 1


async def test_restart_keeps_tasks_periods_jobs_and_history(x):
    # A18
    task = await x.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)))
    ob = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    occ = (await x.all(Occurrence, Occurrence.period_key == "M:2026-10"))[0]
    await x.ok(
        c.MarkPayment(
            obligation_id=ob.data["obligation_id"],
            occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)],
        )
    )
    before_jobs = [(j.dedup_key, j.status) for j in await x.app.jobs()]
    before_agenda = await x.app.read(agenda)

    await x.restart()

    assert (await x.get(Task, task.data["task_id"])).due_date == date(2026, 10, 15)
    assert (await x.get(Occurrence, occ.id)).status == "paid"
    assert [(j.dedup_key, j.status) for j in await x.app.jobs()] == before_jobs
    assert await x.app.read(agenda) == before_agenda
    assert len(await x.all(CommandReceipt)) == 3
    # And the restarted process keeps working on the same data.
    x.clock.set(at(2026, 10, 16))
    late = await x.app.read(agenda)
    assert [i.title for i in late.overdue] == ["Ответить Юле"]


async def test_context_requires_aware_time():
    with pytest.raises(ValueError, match="aware"):
        CommandContext(
            command_id="x", source="test", received_at=at(2026, 1, 1).replace(tzinfo=None)
        )
