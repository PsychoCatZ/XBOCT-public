"""The model is slow or answers late: scheduler/sender keep working, versions are
re-checked after the answer, «да» applies only the current preview."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from tests.conftest import at
from xboct.application import commands as c
from xboct.application.intents import bounded_context
from xboct.llm.contract import ContextMessage, IntentEnvelope, IntentRequest
from xboct.llm.provider import ProviderMeta
from xboct.storage.models import Payment, PendingIntent, Task


class GatedProvider:
    """Waits until released, like a slow model; then answers with `answer(request)`."""

    name = "gated"

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[IntentRequest] = []

    async def propose(self, request: IntentRequest) -> IntentEnvelope:
        return (await self.propose_with_meta(request))[0]

    async def propose_with_meta(self, request: IntentRequest):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        raw = self.answer(request) if callable(self.answer) else self.answer
        return IntentEnvelope.model_validate(raw), ProviderMeta(self.name, "gated")


def use(bot, provider) -> None:
    bot.runtime.intents.provider = provider
    bot.runtime.intents.timeout_seconds = 60


async def test_slow_model_does_not_stop_reminders(bot):
    await bot.ok(
        c.CreateTask(
            title="Оплатить счёт",
            due_date=date(2026, 10, 8),
            due_time=time(15, 0),
        )
    )
    slow = GatedProvider({"kind": "query", "query": {"kind": "agenda"}})
    use(bot, slow)
    bot.say("что на неделе?")
    busy = asyncio.create_task(bot.tick())  # stores the update and starts waiting for the model
    await asyncio.wait_for(slow.started.wait(), 5)
    assert not busy.done()

    bot.clock.set(at(2026, 10, 8, 15, 0))
    report = await asyncio.wait_for(bot.tick(), 5)  # does not wait for the model
    assert report.processed == 0
    assert bot.sent == ["Срок сегодня: «Оплатить счёт» (8 октября (чт) в 15:00)."]

    slow.release.set()
    await asyncio.wait_for(busy, 5)
    assert bot.sent[-1].startswith("Сегодня:") or "Оплатить счёт" in bot.sent[-1]
    assert len(bot.sent) == 2


async def test_record_changed_while_model_thinks_is_not_overwritten(bot):
    result = await bot.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 15)))
    task_id = result.data["task_id"]
    slow = GatedProvider(
        lambda req: {
            "kind": "actions",
            "actions": [
                {
                    "type": "update_task",
                    "task_id": req.candidates[0].id,
                    "next_action": "Отправить вечером",
                }
            ],
        }
    )
    use(bot, slow)
    bot.say("ответ готов, отправлю вечером")
    busy = asyncio.create_task(bot.tick())
    await asyncio.wait_for(slow.started.wait(), 5)
    assert slow.requests[0].candidates[0].version == 1
    # Meanwhile the owner completes the task (e.g. from the web form).
    await bot.ok(c.CompleteTask(task_id=task_id, expected_version=1))
    slow.release.set()
    await asyncio.wait_for(busy, 5)
    task = await bot.x.get(Task, task_id)
    assert task.status == "completed"
    assert task.next_action is None
    assert "изменилась" in bot.sent[-1]
    assert "Данные не менял" in bot.sent[-1]


async def test_yes_confirms_only_the_current_preview(bot):
    for title in ("VPS Германия", "VPS Нидерланды"):
        await bot.ok(
            c.CreateObligation(
                title=title,
                kind="calendar",
                recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 10)),
            )
        )
    bot.provider.add(
        "все VPS оплатил",
        lambda req: {
            "kind": "actions",
            "actions": [
                {"type": "mark_payment", "obligation_id": cand.id, "periods": [{"kind": "current"}]}
                for cand in req.candidates
            ],
        },
    )
    bot.say("все VPS оплатил")
    await bot.tick()
    assert bot.sent[-1].startswith("Проверь, что применяю")
    calls = len(bot.provider.calls)
    bot.say("да")  # a bare «да» is answered by the code from the saved plan, no model call
    await bot.tick()
    assert len(bot.provider.calls) == calls
    assert bot.sent[-1].startswith("Применил всё вместе")
    assert len(await bot.rows(Payment)) == 2
    [pending] = await bot.rows(PendingIntent)
    assert pending.status == "confirmed"

    bot.say("да")  # nothing pending any more
    await bot.tick()
    assert len(await bot.rows(Payment)) == 2
    assert len(bot.provider.calls) == calls
    assert bot.sent[-1] == "Это предложение уже применено. Напиши, что добавить."


async def test_yes_after_preview_changed_meanwhile_is_refused(bot):
    for title in ("VPS Германия", "VPS Нидерланды"):
        await bot.ok(
            c.CreateObligation(
                title=title,
                kind="calendar",
                recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 10)),
            )
        )
    bot.provider.add(
        "все VPS оплатил",
        lambda req: {
            "kind": "actions",
            "actions": [
                {"type": "mark_payment", "obligation_id": cand.id, "periods": [{"kind": "current"}]}
                for cand in req.candidates
            ],
        },
    )
    bot.say("все VPS оплатил")
    await bot.tick()
    slow = GatedProvider({"kind": "confirm_pending"})
    use(bot, slow)
    bot.say("да, давай применяй")  # not a bare «да»: goes through the model
    busy = asyncio.create_task(bot.tick())
    await asyncio.wait_for(slow.started.wait(), 5)
    [pending] = await bot.rows(PendingIntent)
    await bot.ok(c.CancelPendingIntent(pending_id=pending.id))  # e.g. «Отмена» pressed
    slow.release.set()
    await asyncio.wait_for(busy, 5)
    assert await bot.rows(Payment) == []
    assert "Предложение уже отменено" in bot.sent[-1]


async def test_dialog_context_reaches_the_model(bot):
    bot.provider.add("привет", {"kind": "query", "query": {"kind": "agenda"}})
    bot.provider.add("а платежи?", {"kind": "query", "query": {"kind": "payments"}})
    bot.say("привет")
    await bot.tick()
    bot.say("а платежи?")
    await bot.tick()
    context = bot.provider.calls[-1].context
    assert [(m.role, m.text) for m in context] == [
        ("user", "привет"),
        ("assistant", "На неделе спокойно."),
    ]


def test_bounded_context_limits_count_and_size():
    now = datetime(2026, 10, 8, tzinfo=UTC)
    many = [
        ContextMessage(role="user", text=f"m{i}", at=now + timedelta(seconds=i)) for i in range(30)
    ]
    kept = bounded_context(many)
    assert len(kept) == 20
    assert kept[-1].text == "m29"
    big = [
        ContextMessage(role="user", text="x" * 5000, at=now + timedelta(seconds=i))
        for i in range(4)
    ]
    assert len(bounded_context(big)) == 2  # 12000 characters at most, newest kept
