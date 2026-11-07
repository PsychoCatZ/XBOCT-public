"""Scheduler -> outbox -> fake Telegram: actuality, buttons, quiet hours, limits,
catch-up, retries and unknown outcomes (A16, A19, A20, A23)."""

import contextlib
from datetime import date, time

from tests.conftest import at
from xboct.application import commands as c
from xboct.storage.models import (
    CommandReceipt,
    Delivery,
    NotificationJob,
    Obligation,
    Occurrence,
    OutboxMessage,
    Payment,
    Task,
)


async def task(bot, title="Ответить Юле", **kw) -> str:
    kw.setdefault("due_date", date(2026, 10, 9))
    return (await bot.ok(c.CreateTask(title=title, **kw))).data["task_id"]


async def test_due_reminder_goes_out_once_with_buttons(bot):
    task_id = await task(bot)
    await bot.tick()
    assert bot.sent == []  # 8 Oct: nothing due yet
    bot.clock.set(at(2026, 10, 9, 10, 0))
    report = await bot.tick()
    assert report.dispatched == 1
    assert bot.sent == ["Срок сегодня: «Ответить Юле» (9 октября (пт))."]
    assert bot.buttons() == [("Готово", f"dt:{task_id}:1"), ("Позже", bot.buttons()[1][1])]
    jobs = await bot.rows(NotificationJob, NotificationJob.subject_id == task_id)
    assert {j.kind: j.status for j in jobs} == {"due": "sent", "overdue": "pending"}
    bot.clock.advance(minutes=1)
    await bot.tick()
    await bot.restart()
    await bot.tick()
    assert len(bot.sent) == 1  # sent with message_id is never resent


async def test_done_button_completes_once(bot):
    task_id = await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    await bot.tick()
    token = bot.buttons()[0][1]
    bot.press(token)
    bot.press(token)  # double click
    await bot.tick()
    assert (await bot.x.get(Task, task_id)).status == "completed"
    assert [t for t in bot.sent if t.startswith("Готово")] == [
        "Готово: «Ответить Юле» выполнено. Запланированные напоминания отменены."
    ] * 2  # each update gets an answer; the second is the stored result, not a new action
    receipts = await bot.rows(CommandReceipt, CommandReceipt.command_id == f"cb:{token}")
    assert len(receipts) == 1
    # The overdue follow-up of a completed task never goes out.
    bot.clock.set(at(2026, 10, 11, 9, 0))
    await bot.tick()
    assert not any("Срок прошёл" in t for t in bot.sent)


async def test_paid_and_snooze_buttons_work_without_model(bot):
    result = await bot.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 11)),
        )
    )
    ob_id = result.data["obligation_id"]
    await bot.tick()  # 8 Oct 10:00: three days before the 11th
    assert bot.sent == ["Через 3 дн. платёж «VPS» за октябрь 2026 (срок 11 октября (вс))."]
    paid, later = bot.buttons()
    bot.press(later[1])
    await bot.tick()
    assert "Срок не трогаю: срок 11 октября" in bot.sent[-1]
    snoozes = [j for j in await bot.rows(NotificationJob, NotificationJob.kind == "snooze")]
    assert [j.scheduled_at for j in snoozes] == [at(2026, 10, 8, 12, 0)]
    bot.press(paid[1])
    await bot.tick()
    october = (
        await bot.rows(
            Occurrence, Occurrence.owner_id == ob_id, Occurrence.period_key == "M:2026-10"
        )
    )[0]
    assert october.status == "paid"
    assert len(await bot.rows(Payment)) == 1
    bot.clock.set(at(2026, 10, 8, 12, 0))
    await bot.tick()  # the snooze of a paid period is dropped
    assert not any(t.startswith("Напоминаю") for t in bot.sent)


async def test_extension_prevents_stale_coverage_reminder(bot):
    result = await bot.ok(
        c.CreateObligation(title="Хостинг", kind="coverage", coverage_until=date(2026, 10, 10))
    )
    ob_id = result.data["obligation_id"]
    bot.clock.set(at(2026, 10, 9, 10, 0))
    await bot.tick()
    assert bot.sent == [
        "«Хостинг»: Сегодня последний оплаченный день. С 10 октября (сб) уже не оплачено."
    ]
    ob = await bot.x.get(Obligation, ob_id)
    await bot.ok(
        c.ExtendCoverage(obligation_id=ob_id, expected_version=ob.version, unit="month", count=1)
    )
    bot.clock.set(at(2026, 10, 11, 9, 0))  # the old follow-up time
    await bot.tick()
    assert len(bot.sent) == 1


async def test_pre_send_check_drops_reminder_that_became_stale_during_retry(bot):
    task_id = await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    bot.transport.fail_next("transient")
    await bot.tick()
    assert bot.sent == []
    t = await bot.x.get(Task, task_id)
    await bot.ok(c.CompleteTask(task_id=task_id, expected_version=t.version))
    bot.clock.advance(minutes=2)
    await bot.tick()
    assert bot.sent == []
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "cancelled"
    assert message.last_error == "stale"


async def test_quiet_hours_hold_automatic_but_not_explicit(bot):
    task_id = await task(bot, due_date=date(2026, 10, 9), due_time=time(23, 30))
    auto_id = await task(bot, title="Счёт", due_date=date(2026, 10, 9))
    await bot.ok(
        c.SnoozeNotification(
            subject_type="task", subject_id=auto_id, until=at(2026, 10, 9, 23, 40), explicit=False
        )
    )
    bot.clock.set(at(2026, 10, 9, 23, 45))
    await bot.tick()
    assert bot.sent == ["Срок сегодня: «Ответить Юле» (9 октября (пт) в 23:30)."]
    held = await bot.rows(
        NotificationJob, NotificationJob.subject_id == auto_id, NotificationJob.kind == "snooze"
    )
    # «позже» landing in quiet hours was moved to their end by the planner already.
    assert [j.scheduled_at for j in held] == [at(2026, 10, 10, 9, 0)]
    jobs = await bot.rows(
        NotificationJob, NotificationJob.subject_id == auto_id, NotificationJob.status == "pending"
    )
    assert all(j.scheduled_at >= at(2026, 10, 10, 9, 0) for j in jobs)
    assert task_id


async def test_one_message_for_several_subjects_and_daily_limit(bot):
    for i in range(3):
        await task(bot, title=f"Дело {i}")
    bot.clock.set(at(2026, 10, 9, 10, 0))
    report = await bot.tick()
    assert report.dispatched == 1
    assert len(bot.sent) == 1
    assert bot.sent[0].startswith("Напоминания:\n• Срок сегодня: «Дело")
    assert len(bot.buttons()) == 6  # done + later for each subject

    # The cap of 4 automatic messages per day: the 5th waits for tomorrow. Balance
    # warnings at 11..14 o'clock: 100 RUB/hour, threshold 5 days before the estimated zero.
    for hour in (11, 12, 13, 14):
        await bot.ok(
            c.CreateObligation(
                title=f"Баланс {hour}",
                kind="balance",
                balance=c.BalanceStart(
                    checkpoint_minor=100 * (110 + hour) * 100, rate_minor_per_day=2400 * 100
                ),
            )
        )
    for hour in (11, 12, 13, 14):
        bot.clock.set(at(2026, 10, 9, hour, 0))
        await bot.tick()
    assert len(bot.sent) == 4
    deferred = await bot.rows(NotificationJob, NotificationJob.defer_reason == "daily_limit")
    assert [j.scheduled_at for j in deferred] == [at(2026, 10, 10, 9, 0)]
    bot.clock.set(at(2026, 10, 10, 9, 0))
    await bot.tick()
    assert bot.sent[-1].startswith("«Баланс 14»")


async def test_restart_after_downtime_sends_one_catch_up(bot):
    # A20 (catch-up part): process down for two days; no avalanche of old reminders.
    for i in range(5):
        await task(bot, title=f"Дело {i}", due_date=date(2026, 10, 9))
    await bot.stop()
    bot.clock.set(at(2026, 10, 11, 3, 0))  # Sunday night, quiet hours
    await bot.start()
    await bot.tick()
    assert bot.sent == []
    bot.clock.set(at(2026, 10, 11, 9, 0))
    await bot.tick()
    assert len(bot.sent) == 1
    assert bot.sent[0].startswith("Пока я был недоступен")
    for i in range(5):
        assert f"«Дело {i}»" in bot.sent[0]
    assert "Срок прошёл" in bot.sent[0]  # latest stage per subject only
    bot.clock.advance(hours=1)
    await bot.tick()
    assert len(bot.sent) == 1


async def test_unknown_outcome_is_retried_late_and_bounded(bot):
    # A23: Telegram may have accepted the message; no immediate resend.
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    bot.transport.fail_next("unknown", unknown_but_delivered=True)
    await bot.tick()
    assert len(bot.sent) == 1  # it did arrive, we just do not know it
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "pending"
    assert message.next_attempt_at == at(2026, 10, 9, 10, 30)
    bot.clock.advance(minutes=10)
    await bot.tick()
    assert len(bot.sent) == 1
    bot.clock.advance(minutes=21)
    bot.transport.fail_next("unknown")
    await bot.tick()
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "unknown"  # final after two attempts; never claimed as sent
    bot.clock.advance(hours=2)
    await bot.tick()
    assert len(bot.sent) == 1
    attempts = await bot.rows(Delivery, Delivery.outbox_id == message.id)
    assert [a.status for a in attempts] == ["unknown", "unknown"]


async def test_crash_while_sending_is_unknown_after_restart(bot):
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    original = bot.transport.send_message

    async def die(*args, **kwargs):
        raise SystemExit  # the process dies inside the HTTP call

    bot.transport.send_message = die
    with contextlib.suppress(SystemExit):
        await bot.tick()
    bot.transport.send_message = original
    bot.clock.advance(minutes=5)
    await bot.restart()
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "pending"
    assert message.last_error.startswith("unknown")
    assert message.next_attempt_at == at(2026, 10, 9, 10, 35)
    await bot.tick()
    assert bot.sent == []
    bot.clock.advance(minutes=31)
    await bot.tick()
    assert len(bot.sent) == 1


async def test_transient_backoff_permanent_and_retry_after(bot):
    await task(bot)
    await task(bot, title="Второе", due_date=date(2026, 10, 10))
    bot.clock.set(at(2026, 10, 9, 10, 0))
    for _ in range(4):
        bot.transport.fail_next("transient")
    await bot.tick()
    for minutes in (1, 5, 30):
        bot.clock.advance(minutes=minutes)
        await bot.tick()
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "failed"
    assert message.attempts == 4
    jobs = await bot.rows(NotificationJob, NotificationJob.outbox_id == message.id)
    assert {j.status for j in jobs} == {"failed"}

    bot.clock.set(at(2026, 10, 10, 10, 0))
    bot.transport.fail_next("retry_after", retry_after=15)
    await bot.tick()
    bot.clock.advance(seconds=16)
    await bot.tick()
    assert bot.sent[-1].startswith("Срок сегодня: «Второе»")
    sent = (await bot.outbox(OutboxMessage.status == "sent"))[-1]
    assert sent.attempts == 1  # flood control is not a failed attempt


async def test_permanent_error_marks_failed_visible(bot):
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    bot.transport.fail_next("permanent")
    await bot.tick()
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "failed"
    assert message.last_error.startswith("permanent")


async def test_pending_outbox_survives_restart(bot):
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    bot.transport.fail_next("transient")
    await bot.tick()
    await bot.restart()
    bot.clock.advance(minutes=1)
    await bot.tick()
    assert bot.sent == ["Срок сегодня: «Ответить Юле» (9 октября (пт))."]


async def test_later_button_reminds_again_the_same_day(bot):
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    await bot.tick()
    bot.press(bot.buttons()[1][1])  # «Позже»
    await bot.tick()
    bot.clock.set(at(2026, 10, 9, 12, 0))
    await bot.tick()
    assert bot.sent[-1] == "Напоминаю: «Ответить Юле», срок 9 октября (пт)."


async def test_catch_up_respects_daily_limit_and_drops_stale_later(bot):
    # Codex review 2: the budget of automatic messages is already used up today.
    task_id = await task(bot, title="Старое", due_date=date(2026, 10, 8))
    async with bot.app.sessions() as session, session.begin():
        for i in range(4):
            session.add(
                OutboxMessage(
                    kind="notification",
                    chat_id=1,
                    text=f"earlier {i}",
                    buttons=[],
                    dedup_key=f"earlier:{i}",
                    subjects=[],
                    auto=True,
                    local_date=date(2026, 10, 8),
                    status="sent",
                    attempts=1,
                    next_attempt_at=bot.clock.now(),
                    created_at=bot.clock.now(),
                    sent_at=bot.clock.now(),
                )
            )
    bot.clock.set(at(2026, 10, 8, 17, 0))  # the 10:00 reminder is 7 hours late
    report = await bot.tick()
    assert bot.sent == []
    assert report.deferred >= 1
    [job] = await bot.rows(
        NotificationJob, NotificationJob.subject_id == task_id, NotificationJob.kind == "due"
    )
    assert (job.status, job.defer_reason) == ("pending", "daily_limit")
    assert job.scheduled_at == at(2026, 10, 9, 9, 0)
    assert job.planned_at == at(2026, 10, 8, 10, 0)  # data kept as planned

    t = await bot.x.get(Task, task_id)
    await bot.ok(c.CompleteTask(task_id=task_id, expected_version=t.version))
    bot.clock.set(at(2026, 10, 9, 9, 0))
    await bot.tick()
    assert bot.sent == []
    job = (await bot.rows(NotificationJob, NotificationJob.id == job.id))[0]
    assert job.status == "cancelled"


async def test_catch_up_waits_for_budget_then_goes_out(bot):
    task_id = await task(bot, title="Старое", due_date=date(2026, 10, 8))
    async with bot.app.sessions() as session, session.begin():
        for i in range(4):
            session.add(
                OutboxMessage(
                    kind="notification",
                    chat_id=1,
                    text=f"earlier {i}",
                    buttons=[],
                    dedup_key=f"earlier:{i}",
                    subjects=[],
                    auto=True,
                    local_date=date(2026, 10, 8),
                    status="sent",
                    attempts=1,
                    next_attempt_at=bot.clock.now(),
                    created_at=bot.clock.now(),
                    sent_at=bot.clock.now(),
                )
            )
    bot.clock.set(at(2026, 10, 8, 17, 0))
    await bot.tick()
    bot.clock.set(at(2026, 10, 9, 9, 0))
    await bot.tick()
    assert len(bot.sent) == 1
    assert "«Старое»" in bot.sent[0]
    assert task_id


async def test_immediate_restart_after_crash_mid_send_does_not_hang(bot):
    # Codex review 3: restart BEFORE the lease expires; the row must not stay `sending`.
    await task(bot)
    bot.clock.set(at(2026, 10, 9, 10, 0))
    original = bot.transport.send_message

    async def die(*args, **kwargs):
        raise SystemExit

    bot.transport.send_message = die
    with contextlib.suppress(SystemExit):
        await bot.tick()
    bot.transport.send_message = original
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "sending"
    assert message.lease_until > bot.clock.now()  # lease still valid
    await bot.restart()  # same instant
    [message] = await bot.outbox(OutboxMessage.kind == "notification")
    assert message.status == "pending"
    assert message.last_error.startswith("unknown")
    assert message.next_attempt_at == at(2026, 10, 9, 10, 30)  # unknown: delayed retry
    [attempt] = await bot.rows(Delivery, Delivery.outbox_id == message.id)
    assert attempt.status == "unknown"
    await bot.tick()
    assert bot.sent == []
    bot.clock.set(at(2026, 10, 9, 10, 31))
    await bot.tick()
    assert bot.sent == ["Срок сегодня: «Ответить Юле» (9 октября (пт))."]
