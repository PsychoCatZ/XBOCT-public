"""Weekly digest, daily refresh, single instance (A20, A21, A22, A30, A32)."""

import asyncio
from datetime import date

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from tests.conftest import at
from tests.integration.rt import OWNER
from xboct.application import commands as c
from xboct.application.service import Application
from xboct.scheduling.lock import AlreadyRunning
from xboct.scheduling.runtime import Runtime
from xboct.scheduling.state import LAST_REFRESH, get_state
from xboct.storage.models import DigestRun, NotificationJob, Occurrence, OutboxMessage
from xboct.telegram.aiogram_transport import classify_send_error
from xboct.telegram.transport import FakeTransport

MONDAY_10 = at(2026, 10, 12, 10, 0)


def digests(bot) -> list[str]:
    return [t for t in bot.sent if t.startswith("Сводка на неделю")]


async def test_monday_digest_once_even_with_retry_and_restart(bot):
    # A21
    await bot.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 14)))
    bot.clock.set(MONDAY_10)
    bot.transport.fail_next("transient")
    await bot.tick()
    assert digests(bot) == []
    await bot.restart()
    bot.clock.advance(minutes=1)
    await bot.tick()
    assert len(digests(bot)) == 1
    text = digests(bot)[0]
    assert text.startswith("Сводка на неделю 12 октября (пн) — 18 октября (вс)\n")
    assert "с задержкой" not in text
    assert "«Ответить Юле» — 14 октября (ср)" in text
    for minutes in (30, 60, 600):
        bot.clock.advance(minutes=minutes)
        await bot.tick()
    await bot.restart()
    await bot.tick()
    assert len(digests(bot)) == 1
    assert len(await bot.rows(DigestRun)) == 1


async def test_empty_week_is_short(bot):
    bot.clock.set(MONDAY_10)
    await bot.tick()
    assert digests(bot)[0].endswith("На неделе спокойно.")


async def test_down_on_monday_one_delayed_digest_tuesday_after_quiet(bot):
    # A20: back on Tuesday at night; quiet hours first, then one digest marked as delayed
    # that also absorbs the missed reminders: no avalanche.
    for i in range(3):
        await bot.ok(c.CreateTask(title=f"Дело {i}", due_date=date(2026, 10, 12)))
    await bot.stop()
    bot.clock.set(at(2026, 10, 13, 2, 0))
    await bot.start()
    await bot.tick()
    assert bot.sent == []
    bot.clock.set(at(2026, 10, 13, 9, 0))
    await bot.tick()
    assert len(bot.sent) == 1
    [digest] = digests(bot)
    assert "(с задержкой" in digest
    assert "Срок прошёл, отметки нет:" in digest
    assert all(f"«Дело {i}»" in digest for i in range(3))
    for hours in (1, 5, 24):
        bot.clock.advance(hours=hours)
        await bot.tick()
    assert len(digests(bot)) == 1


async def test_confirmed_digest_suppresses_duplicate_but_keeps_exact_reminder(bot):
    # A22
    plain = await bot.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 12)))
    exact = await bot.ok(c.CreateTask(title="Созвон", due_date=date(2026, 10, 12)))
    await bot.ok(
        c.SnoozeNotification(
            subject_type="task", subject_id=exact.data["task_id"], until=at(2026, 10, 12, 10, 0)
        )
    )
    bot.clock.set(MONDAY_10)
    await bot.tick()
    assert len(digests(bot)) == 1
    assert bot.sent[0].startswith("Сводка")
    assert bot.sent[1:] == ["Напоминаю: «Созвон», срок 12 октября (пн)."]
    [job] = await bot.rows(
        NotificationJob,
        NotificationJob.subject_id == plain.data["task_id"],
        NotificationJob.kind == "due",
    )
    assert (job.status, job.cancel_reason) == ("suppressed", "digest_covered")


async def test_failed_digest_suppresses_nothing(bot):
    await bot.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 12)))
    bot.clock.set(MONDAY_10)
    bot.transport.fail_next("permanent")
    await bot.tick()
    assert bot.sent == ["Срок сегодня: «Счёт» (12 октября (пн))."]
    [digest] = await bot.outbox(OutboxMessage.kind == "digest")
    assert digest.status == "failed"


async def test_digest_history_only_from_real_deliveries(bot):
    # A32: «напоминание отправлялось» only after a confirmed delivery.
    sent = await bot.ok(c.CreateTask(title="Отправленное", due_date=date(2026, 10, 9)))
    lost = await bot.ok(c.CreateTask(title="Потерянное", due_date=date(2026, 10, 10)))
    bot.clock.set(at(2026, 10, 9, 10, 0))
    await bot.tick()
    bot.clock.set(at(2026, 10, 10, 10, 0))
    bot.transport.fail_next("permanent")
    await bot.tick()
    for task_id in (sent.data["task_id"], lost.data["task_id"]):
        await bot.ok(c.UpdateTask(task_id=task_id, expected_version=1, note="n"))
    bot.clock.set(MONDAY_10)
    await bot.tick()
    digest = digests(bot)[0]
    assert "«Отправленное» — 9 октября (пт) (напоминание отправлялось 9 октября (пт))" in digest
    assert "«Потерянное» — 10 октября (сб)\n" in digest + "\n"


async def test_digest_lists_last_payment_from_history(bot):
    result = await bot.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 9)),
        )
    )
    ob_id = result.data["obligation_id"]
    occ = (
        await bot.rows(
            Occurrence, Occurrence.owner_id == ob_id, Occurrence.period_key == "M:2026-10"
        )
    )[0]
    await bot.ok(
        c.MarkPayment(
            obligation_id=ob_id, occurrences=[c.OccurrenceRef(id=occ.id, expected_version=1)]
        )
    )
    bot.clock.set(MONDAY_10)
    await bot.tick()
    assert "Спокойно: «VPS» — следующий срок 9 ноября (пн)." in digests(bot)[0]


async def test_refresh_at_start_then_once_per_local_day_and_retry_after_failure(bot, monkeypatch):
    async with bot.app.sessions() as session:
        assert (await get_state(session, LAST_REFRESH))["local_date"] == "2026-10-08"
    assert (await bot.tick()).refreshed is False
    bot.clock.set(at(2026, 10, 9, 0, 1))  # local midnight passed (Krasnoyarsk)
    calls = []
    original = Application.refresh_schedule

    async def failing(self, command_id=None):
        calls.append(command_id)
        raise RuntimeError("disk full")

    monkeypatch.setattr(Application, "refresh_schedule", failing)
    assert (await bot.tick()).refreshed is False
    assert (await bot.tick()).refreshed is False
    assert len(calls) == 2  # retried, not forgotten
    monkeypatch.setattr(Application, "refresh_schedule", original)
    assert (await bot.tick()).refreshed is True
    assert (await bot.tick()).refreshed is False
    async with bot.app.sessions() as session:
        assert (await get_state(session, LAST_REFRESH))["local_date"] == "2026-10-09"


async def test_second_instance_refuses_to_start(bot, db_url):
    # A30
    other_app = await Application.open(db_url, bot.clock)
    other = Runtime(other_app, FakeTransport(), bot.provider, OWNER, lock_path=bot.lock)
    with pytest.raises(AlreadyRunning):
        await other.start()
    with pytest.raises(RuntimeError):
        await other.tick()
    await bot.stop()
    await other.start()  # the first one is gone: now it may run
    await other.stop()
    await other_app.close()
    await bot.start()


def test_aiogram_error_classification():
    method = SendMessage(chat_id=1, text="x")
    assert classify_send_error(TelegramRetryAfter(method, "flood", 7)).kind == "retry_after"
    assert classify_send_error(TelegramRetryAfter(method, "flood", 7)).retry_after == 7
    assert classify_send_error(TelegramServerError(method, "502")).kind == "transient"
    assert classify_send_error(TelegramForbiddenError(method, "blocked")).kind == "permanent"
    assert classify_send_error(TelegramBadRequest(method, "bad")).kind == "permanent"
    assert classify_send_error(TelegramNetworkError(method, "reset")).kind == "unknown"
    assert classify_send_error(TimeoutError()).kind == "unknown"


async def test_run_loop_polls_ticks_and_stops(bot):
    await bot.stop()
    bot.provider.add("привет", {"kind": "query", "query": {"kind": "agenda"}})
    bot.say("привет")
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    for _ in range(100):
        if bot.transport.sent:
            break
        await asyncio.sleep(0.02)
    await runtime.stop()
    await asyncio.wait_for(task, 5)
    await app.close()
    assert bot.sent == ["На неделе спокойно."]
    assert not runtime.lock.held
    await bot.start()


async def test_missed_reminder_survives_undelivered_digest(bot):
    # Codex review 1: a missed reminder must not be lost when the digest of the same
    # tick is not delivered.
    task_id = (await bot.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 11)))).data[
        "task_id"
    ]
    await bot.stop()
    bot.clock.set(MONDAY_10)  # down since Thursday: the 11 Oct reminder was missed
    await bot.start()
    bot.transport.fail_next("permanent")
    await bot.tick()
    [digest] = await bot.outbox(OutboxMessage.kind == "digest")
    assert digest.status == "failed"
    assert len(bot.sent) == 1  # the regular catch-up went out instead
    assert bot.sent[0].startswith("Пока я был недоступен")
    assert "«Счёт»" in bot.sent[0]
    [due] = await bot.rows(
        NotificationJob, NotificationJob.subject_id == task_id, NotificationJob.kind == "due"
    )
    assert due.status == "sent"


async def test_reminder_riding_on_digest_is_released_when_digest_finally_fails(bot):
    task_id = (await bot.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 11)))).data[
        "task_id"
    ]
    await bot.stop()
    bot.clock.set(MONDAY_10)
    await bot.start()
    for _ in range(4):
        bot.transport.fail_next("transient")
    await bot.tick()  # digest pending retry; the missed reminder rides on it
    [due] = await bot.rows(
        NotificationJob, NotificationJob.subject_id == task_id, NotificationJob.kind == "due"
    )
    [digest] = await bot.outbox(OutboxMessage.kind == "digest")
    assert (due.status, due.outbox_id) == ("dispatched", digest.id)
    for minutes in (1, 5, 30):
        bot.clock.advance(minutes=minutes)
        await bot.tick()
    [digest] = await bot.outbox(OutboxMessage.kind == "digest")
    assert digest.status == "failed"
    # Released after the final failure and delivered as a catch-up on the next pass.
    [due] = await bot.rows(NotificationJob, NotificationJob.id == due.id)
    assert due.status in ("pending", "sent")
    bot.clock.advance(minutes=1)
    await bot.tick()
    assert any(t.startswith("Пока я был недоступен") and "«Счёт»" in t for t in bot.sent)
    [due] = await bot.rows(NotificationJob, NotificationJob.id == due.id)
    assert due.status == "sent"
    assert digests(bot) == []


async def test_missed_reminder_rides_on_delivered_digest(bot):
    task_id = (await bot.ok(c.CreateTask(title="Счёт", due_date=date(2026, 10, 11)))).data[
        "task_id"
    ]
    await bot.stop()
    bot.clock.set(MONDAY_10)
    await bot.start()
    await bot.tick()
    assert len(bot.sent) == 1
    assert "«Счёт»" in digests(bot)[0]
    [due] = await bot.rows(
        NotificationJob, NotificationJob.subject_id == task_id, NotificationJob.kind == "due"
    )
    assert due.status == "sent"
