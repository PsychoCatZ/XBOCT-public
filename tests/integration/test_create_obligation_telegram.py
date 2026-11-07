"""The owner's Demo chain through the real Telegram path (inbox, router, outbox).

Each message is a Telegram update; the model receives the recent dialogue as context. The
chain is the transcript's order of events, with the saved preview that was missing.
"""

from __future__ import annotations

from datetime import date

from xboct.storage.models import Obligation, Payment, PendingIntent

FIRST = "Добавь подписку Demo — 17 долларов в месяц. Оплачено по 10.11.2026 включительно"
SECOND = "первая оплата была 10.10.2026, оплачивать необходимо каждый месяц."


def demo(**extra) -> dict:
    return {
        "kind": "actions",
        "actions": [
            {
                "type": "create_obligation",
                "title": "Demo",
                "kind": "coverage",
                "amount": "17",
                "currency": "USD",
                "price_period": "month",
                "paid_through": {"kind": "last_paid_day", "day": "2026-11-10"},
                **extra,
            }
        ],
    }


async def test_the_chain_gives_a_preview_then_yes_creates_exactly_one_record(bot):
    bot.provider.add(FIRST, demo())
    bot.provider.add(SECOND, demo(first_payment_on={"kind": "date", "value": "2026-10-10"}))
    bot.provider.add(
        "Да",
        lambda req: (
            {"kind": "confirm_pending"}
            if req.pending_preview
            else {"kind": "clarification", "clarification": {"question": "Что «да»?"}}
        ),
    )

    bot.say(FIRST)
    await bot.tick()
    assert bot.sent[-1].startswith("Проверь, что применяю")
    assert "новая подписка «Demo»" in bot.sent[-1]
    assert [text for text, _ in bot.buttons()] == ["Подтвердить", "Отмена"]
    assert await bot.rows(Obligation) == []
    [first_preview] = await bot.rows(PendingIntent)
    assert first_preview.status == "pending"

    bot.say(SECOND)  # more facts after the preview: a new preview replaces the old one
    await bot.tick()
    context = bot.provider.calls[-1].context
    assert [m.role for m in context] == ["user", "assistant"]
    assert context[0].text == FIRST
    assert bot.provider.calls[-1].pending_preview is not None
    assert "Первая оплата: 10 октября 2026" in bot.sent[-1]
    assert await bot.rows(Obligation) == []

    bot.say("Да")
    await bot.tick()
    request = bot.provider.calls[-1]
    assert "новая подписка «Demo»" in request.pending_preview
    assert "Завёл «Demo»" in bot.sent[-1]
    [demo_row] = await bot.rows(Obligation)
    assert (demo_row.currency, demo_row.amount_minor) == ("USD", 1700)
    assert demo_row.coverage_until == date(2026, 11, 11)
    assert "Первая оплата: 10 октября 2026" in demo_row.note
    assert await bot.rows(Payment) == []
    statuses = sorted(p.status for p in await bot.rows(PendingIntent))
    assert statuses == ["confirmed", "superseded"]

    calls = len(bot.provider.calls)
    bot.say("Да")  # a second «Да»: nothing is waiting any more; the code answers
    await bot.tick()
    assert len(await bot.rows(Obligation)) == 1
    assert bot.sent[-1] == "Это предложение уже применено. Напиши, что добавить."
    assert len(bot.provider.calls) == calls  # no model call for a bare «Да»


async def test_the_confirm_button_does_the_same_as_the_word_yes(bot):
    bot.provider.add(FIRST, demo())
    bot.say(FIRST)
    await bot.tick()
    confirm = next(data for text, data in bot.buttons() if text == "Подтвердить")
    bot.press(confirm)
    await bot.tick()
    assert "Завёл «Demo»" in bot.sent[-1]
    assert len(await bot.rows(Obligation)) == 1
    bot.press(confirm)  # the same button again: no second record
    await bot.tick()
    assert len(await bot.rows(Obligation)) == 1


async def test_yes_after_a_model_clarification_creates_nothing(bot):
    """The transcript's real failure, through Telegram: the model asked «Создать …?» as a
    clarification. «Да» must not create anything, and must not claim anything happened."""
    bot.provider.add(
        "Добавь подписку Demo — 17 долларов в месяц.",
        {
            "kind": "clarification",
            "clarification": {"question": "Создать подписку Demo?", "candidate_ids": []},
        },
    )
    bot.provider.add("Да", {"kind": "confirm_pending"})
    bot.say("Добавь подписку Demo — 17 долларов в месяц.")
    await bot.tick()
    bot.say("Да")
    await bot.tick()
    assert bot.sent[-1] == "Сохранённого предложения нет. Напиши, что добавить."
    assert await bot.rows(Obligation) == []
    assert await bot.rows(PendingIntent) == []
