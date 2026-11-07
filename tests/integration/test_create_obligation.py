"""Creating a subscription or a calendar payment from text, always through a saved preview.

The original failure (owner's Demo dialogue): the model could not propose the creation, asked
a «Создать …?» question as a clarification, and the owner's «Да» had nothing to confirm.
Here the model proposes `create_obligation`, the code stores a preview first, and «Да»
confirms exactly that preview. The model is a scripted fixture; the pipeline is real.
"""

from __future__ import annotations

from datetime import date

import pytest

from xboct.application import commands as c
from xboct.llm.contract import ContextMessage
from xboct.storage.models import Obligation, Payment, PendingIntent
from xboct.style import formal_markers

DEMO_TEXT = "Добавь подписку Demo — 17 долларов в месяц. Оплачено по 10.11.2026 включительно"


def demo(**over) -> dict:
    action = {
        "type": "create_obligation",
        "title": "Demo",
        "kind": "coverage",
        "amount": "17",
        "currency": "USD",
        "price_period": "month",
        "paid_through": {"kind": "last_paid_day", "day": "2026-11-10"},
        **over,
    }
    return {"kind": "actions", "actions": [action]}


def reply(kind: str) -> dict:
    return {"kind": kind}


async def obligations(x) -> list[Obligation]:
    return await x.all(Obligation)


async def pendings(x, status: str = "pending") -> list[PendingIntent]:
    return await x.all(PendingIntent, PendingIntent.status == status)


async def say(x, text: str, *, context=(), command_id: str | None = None):
    ctx = x.ctx(command_id, "telegram_update")
    return await x.intents.handle_message(text, ctx, context=list(context))


def formal(result) -> list[str]:
    return formal_markers(result.message)


def plain(result) -> str:
    """The reply with non-breaking spaces turned into spaces (amounts are formatted with them)."""
    return result.message.replace("\xa0", " ")


# --- the Demo dialogue ---------------------------------------------------------------------


async def test_a_clear_first_message_gives_a_saved_preview_then_yes_creates_one_record(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))

    preview = await say(x, DEMO_TEXT)
    assert preview.status == "preview"
    # The preview is saved before the text asking to confirm it exists.
    [pending] = await pendings(x)
    assert pending.actions[0]["type"] == "create_obligation"
    assert pending.actions[0]["amount_minor"] == 1700
    assert await obligations(x) == []  # nothing created yet
    assert "новая подписка «Demo»: оплачено по 10 ноября (вт) включительно" in plain(preview)
    assert "сумма 17 $" in plain(preview)
    assert "Цена: 17 $ в месяц" in plain(preview)
    assert "Подтвердить?" in preview.message
    assert formal(preview) == []

    done = await say(x, "Да")
    assert done.status == "applied"
    [ob] = await obligations(x)
    assert (ob.title, ob.kind, ob.currency, ob.amount_minor) == (
        "Demo",
        "coverage",
        "USD",
        1700,
    )
    assert ob.coverage_until == date(2026, 11, 11)  # the first unpaid day, computed by the code
    assert ob.recurrence_id is None  # a subscription has no recurrence rule
    assert ob.note == "Цена: 17 $ в месяц"
    assert await x.all(Payment) == []  # no invented payment
    [stored] = await x.all(PendingIntent)
    assert stored.status == "confirmed"


async def test_reminders_follow_the_stored_boundary(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    await say(x, "Да")
    [ob] = await obligations(x)
    jobs = {j.kind: j for j in await x.pending_jobs(ob.id)}
    assert set(jobs) >= {"advance", "due", "overdue"}
    tz = await x.app.read(lambda uow: _tz(uow))
    assert jobs["due"].planned_at.astimezone(tz).date() == date(2026, 11, 10)  # last paid day
    assert jobs["advance"].planned_at.astimezone(tz).date() == date(2026, 11, 8)  # 3 days before
    assert jobs["overdue"].planned_at.astimezone(tz).date() >= date(2026, 11, 11)


async def _tz(uow):
    return uow.tz


async def test_redelivered_yes_does_not_create_a_second_record(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    first = await say(x, "Да", command_id="tg-update:900")
    again = await say(x, "Да", command_id="tg-update:900")  # the same Telegram update
    assert first.status == "applied"
    assert again.replayed
    assert again.message == first.message
    assert len(await obligations(x)) == 1


async def test_no_cancels_the_preview_and_creates_nothing(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Нет", reply("cancel_pending"))
    await say(x, DEMO_TEXT)
    result = await say(x, "Нет")
    assert result.message == "Ок, ничего не меняю."
    assert await obligations(x) == []
    assert len(await pendings(x, "cancelled")) == 1
    assert await pendings(x) == []


async def test_yes_without_a_saved_preview_says_so(x):
    x.provider.add("Да", reply("confirm_pending"))
    result = await say(x, "Да")
    assert result.status == "clarification"
    assert result.message == "Сохранённого предложения нет. Напиши, что добавить."
    assert await obligations(x) == []


async def test_yes_after_an_expired_preview_says_it_is_stale(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    x.clock.advance(minutes=31)
    result = await say(x, "Да")
    assert result.message == "Предложение устарело. Напиши, что добавить, ещё раз."
    assert await obligations(x) == []


async def test_a_replaced_preview_confirms_only_the_newest(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("то же, но Demo Pro", demo(title="Demo Pro"))
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    await say(x, "то же, но Demo Pro")
    assert len(await pendings(x, "superseded")) == 1
    await say(x, "Да")
    assert [o.title for o in await obligations(x)] == ["Demo Pro"]


async def test_the_original_transcript_never_asks_to_confirm_without_a_saved_preview(x):
    """What the model did: two clarifications, the second a «Создать …?» question. The
    application must not treat «Да» as a confirmation of anything it did not store."""
    x.provider.add(
        "Добавь подписку Demo — 17 долларов в месяц.",
        {
            "kind": "clarification",
            "clarification": {
                "question": "Нужно добавить подписку Demo?",
                "candidate_ids": [],
            },
        },
    )
    x.provider.add(
        "Да",
        {"kind": "confirm_pending"},
    )
    first = await say(x, "Добавь подписку Demo — 17 долларов в месяц.")
    assert first.status == "clarification"
    assert await pendings(x) == []  # a clarification is not a preview
    answer = await say(x, "Да")
    assert answer.message == "Сохранённого предложения нет. Напиши, что добавить."
    assert await obligations(x) == []
    assert "Создал" not in answer.message
    assert "Завёл" not in answer.message


# --- incomplete commands: a content question, the owner's answer, a preview, a confirmation ---


async def test_a_missing_boundary_is_asked_then_answered_with_the_dialogue_in_context(x):
    first_text = "Добавь подписку Demo — 17 долларов в месяц"
    second_text = "по 10.11.2026 включительно"
    seen: list[list[ContextMessage]] = []

    x.provider.add(first_text, demo(paid_through=None))

    def second(request):
        seen.append(request.context)
        return demo()

    x.provider.add(second_text, second)
    x.provider.add("Да", reply("confirm_pending"))

    asked = await say(x, first_text)
    assert asked.status == "clarification"
    assert asked.message == "До какого дня «Demo» уже оплачена? Назови последний оплаченный день."
    assert formal(asked) == []
    assert await pendings(x) == []

    now = x.clock.now()
    dialog = [
        ContextMessage(role="user", text=first_text, at=now),
        ContextMessage(role="assistant", text=asked.message, at=now),
    ]
    preview = await say(x, second_text, context=dialog)
    assert preview.status == "preview"
    assert [m.role for m in seen[0]] == ["user", "assistant"]
    assert len(await pendings(x)) == 1

    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.coverage_until == date(2026, 11, 11)


# --- the boundary is computed by the code --------------------------------------------------


@pytest.mark.parametrize(
    ("through", "expected"),
    [
        ({"kind": "last_paid_day", "day": "2026-11-10"}, date(2026, 11, 11)),
        ({"kind": "last_paid_day", "day": "2026-12-31"}, date(2027, 1, 1)),  # year border
        ({"kind": "last_paid_day", "day": "2026-10-31"}, date(2026, 11, 1)),  # month border
        ({"kind": "last_paid_day", "day": "2028-02-28"}, date(2028, 2, 29)),  # leap day
        ({"kind": "last_paid_day", "day": "2028-02-29"}, date(2028, 3, 1)),
        ({"kind": "first_unpaid_day", "day": "2026-11-10"}, date(2026, 11, 10)),
    ],
)
async def test_coverage_until_is_derived_from_the_stated_day(x, through, expected):
    x.provider.add("создай", demo(paid_through=through))
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, "создай")
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.coverage_until == expected


@pytest.mark.parametrize(
    ("through", "expected_text", "code"),
    [
        (
            {"kind": "unclear", "day": "2026-11-10"},
            "«До 10 ноября (вт)»: этот день уже оплачен или с него снова нужно платить?",
            "coverage_boundary_unclear",
        ),
        (
            {"kind": "month_only", "month": 1},
            "«До января» можно понять по-разному. Назови точную дату: последний оплаченный "
            "день включительно.",
            "coverage_boundary_vague",
        ),
    ],
)
async def test_an_unclear_boundary_is_a_question_and_creates_nothing(
    x, through, expected_text, code
):
    x.provider.add("создай", demo(paid_through=through))
    result = await say(x, "создай")
    assert result.status == "clarification"
    assert result.code == code
    assert result.message == expected_text
    assert formal(result) == []
    assert await pendings(x) == []
    assert await obligations(x) == []


# --- money and currency --------------------------------------------------------------------


async def test_an_amount_without_a_currency_is_asked_about(x):
    x.provider.add("создай", demo(currency=None))
    result = await say(x, "создай")
    assert result.status == "clarification"
    assert result.message == "Уточни валюту: 17 — рубли, доллары или евро?"
    assert formal(result) == []
    assert await pendings(x) == []


@pytest.mark.parametrize(("currency", "minor", "symbol"), [("RUB", 1700, "₽"), ("EUR", 1700, "€")])
async def test_named_currencies_are_kept(x, currency, minor, symbol):
    x.provider.add("создай", demo(currency=currency))
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "создай")
    assert symbol in preview.message
    await say(x, "Да")
    [ob] = await obligations(x)
    assert (ob.currency, ob.amount_minor) == (currency, minor)


async def test_no_amount_means_no_amount_and_no_currency_question(x):
    x.provider.add("создай", demo(amount=None, currency=None, price_period=None))
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "создай")
    assert preview.status == "preview"
    assert "сумма" not in preview.message
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.amount_minor is None
    assert ob.note is None


async def test_a_price_period_without_an_amount_invents_nothing(x):
    x.provider.add("создай", demo(amount=None, currency=None))  # «в месяц» but no price
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, "создай")
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.note is None


# --- notes: the owner's text first and untouched; stated facts as code lines ---------------


async def test_the_owners_note_is_kept_verbatim_and_first(x):
    own = "Оформлено на Юлю, не вы и не они"
    x.provider.add(
        "создай",
        demo(note=own, first_payment_on={"kind": "date", "value": "2026-10-10"}),
    )
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "создай")
    assert "записи об оплате не создаю" in preview.message
    await say(x, "Да")
    [ob] = await obligations(x)
    lines = ob.note.split("\n")
    assert lines[0] == own
    assert lines[1] == "Цена: 17 $ в месяц"
    assert lines[2].startswith("Первая оплата: 10 октября 2026")
    assert await x.all(Payment) == []  # the date is only a note, not a payment record


# --- duplicates ----------------------------------------------------------------------------


async def test_an_existing_record_is_found_and_not_duplicated(x):
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 11, 11)))
    x.provider.add("создай", demo())
    result = await say(x, "создай")
    assert result.status == "clarification"
    assert "«Demo» уже есть (подписка, оплачено по 10 ноября (вт) включительно)" in result.message
    assert "Создать вторую запись или изменить существующую?" in result.message
    assert await pendings(x) == []
    assert len(await obligations(x)) == 1


@pytest.mark.parametrize("existing", ["demo", "  Demo  ", "Demo подписка", "DEMO"])
async def test_name_variants_count_as_duplicates(x, existing):
    await x.ok(
        c.CreateObligation(title=existing, kind="coverage", coverage_until=date(2026, 12, 1))
    )
    x.provider.add("создай", demo())
    result = await say(x, "создай")
    assert result.status == "clarification"
    assert len(await obligations(x)) == 1


async def test_an_alias_counts_as_the_same_name(x):
    await x.ok(
        c.CreateObligation(
            title="Moonshot",
            aliases=["Demo"],
            kind="coverage",
            coverage_until=date(2026, 12, 1),
        )
    )
    x.provider.add("создай", demo())
    assert (await say(x, "создай")).status == "clarification"


async def test_a_different_name_is_not_a_duplicate(x):
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1)))
    x.provider.add("создай", demo(title="Midjourney"))
    assert (await say(x, "создай")).status == "preview"


async def test_an_archived_record_does_not_block_a_new_one(x):
    result = await x.ok(
        c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1))
    )
    await x.ok(
        c.ArchiveObligation(
            obligation_id=result.data["obligation_id"], expected_version=1, outstanding="keep_open"
        )
    )
    x.provider.add("создай", demo())
    assert (await say(x, "создай")).status == "preview"


async def test_a_second_record_only_when_the_owner_asked_for_it(x):
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 11, 11)))
    x.provider.add("создай ещё одну Demo", demo(allow_duplicate=True))
    x.provider.add("Да", reply("confirm_pending"))
    assert (await say(x, "создай ещё одну Demo")).status == "preview"
    await say(x, "Да")
    assert len(await obligations(x)) == 2


async def test_the_existing_record_is_never_renamed_or_changed_by_a_duplicate_request(x):
    result = await x.ok(
        c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 11, 11))
    )
    before = await x.get(Obligation, result.data["obligation_id"])
    x.provider.add("создай", demo())
    await say(x, "создай")
    after = await x.get(Obligation, before.id)
    assert (after.title, after.coverage_until, after.version, after.note) == (
        before.title,
        before.coverage_until,
        before.version,
        before.note,
    )


# --- calendar payments ---------------------------------------------------------------------


def rent(**over) -> dict:
    action = {
        "type": "create_obligation",
        "title": "Аренда",
        "kind": "calendar",
        "amount": "45000",
        "currency": "RUB",
        "recurrence": {"frequency": "monthly", "start": {"kind": "date", "value": "2026-10-20"}},
        **over,
    }
    return {"kind": "actions", "actions": [action]}


async def test_a_calendar_payment_with_an_explicit_rule_is_previewed_then_created(x):
    x.provider.add("аренда", rent())
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "аренда")
    assert preview.status == "preview"
    assert (
        "новый платёж по календарю «Аренда»: каждый месяц, первый срок 20 октября (вт)"
        in preview.message
    )
    assert "сумма 45 000 ₽" in plain(preview)
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.kind == "calendar"
    assert ob.recurrence_id is not None
    assert ob.coverage_until is None


async def test_a_calendar_payment_without_a_rule_is_asked_about(x):
    x.provider.add("аренда", rent(recurrence=None))
    result = await say(x, "аренда")
    assert result.status == "clarification"
    assert result.message == "Назови ближайший срок платежа «Аренда» и как часто он повторяется."
    assert formal(result) == []
    assert await pendings(x) == []


async def test_an_interval_rule_is_described_in_words(x):
    rule = {
        "frequency": "monthly",
        "interval": 3,
        "start": {"kind": "date", "value": "2026-10-20"},
    }
    x.provider.add("аренда", rent(recurrence=rule))
    preview = await say(x, "аренда")
    assert "каждые 3 месяца, первый срок" in preview.message


@pytest.mark.parametrize(
    "bad",
    [
        demo(recurrence={"frequency": "monthly", "start": {"kind": "date", "value": "2026-10-20"}}),
        rent(paid_through={"kind": "last_paid_day", "day": "2026-11-10"}),
    ],
)
async def test_mixing_kinds_is_refused_not_quietly_fixed(x, bad):
    x.provider.add("создай", bad)
    result = await say(x, "создай")
    assert result.status == "rejected"
    assert await pendings(x) == []
    assert await obligations(x) == []


# --- wording -------------------------------------------------------------------------------


async def test_capabilities_tell_the_truth_about_creation(x):
    x.provider.add("что ты умеешь", {"kind": "conversation", "conversation_kind": "capabilities"})
    result = await say(x, "что ты умеешь")
    assert "подписку" in result.message
    assert "спрошу «да»" in result.message
    assert "VPS с балансом заводи в форме" in result.message
    assert formal(result) == []


async def test_the_owners_titles_are_stored_exactly(x):
    title = "Demo, как вы там, пожалуйста"
    x.provider.add("создай", demo(title=title))
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "создай")
    assert f"«{title}»" in preview.message
    assert formal(preview) == []  # the quoted title is the owner's data
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.title == title


def test_the_proposal_schema_is_still_strict_compatible():
    from xboct.llm.contract import IntentEnvelope
    from xboct.llm.schema import check_strict_subset, strict_schema

    assert check_strict_subset(strict_schema(IntentEnvelope)) == []
