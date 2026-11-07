"""Review of the first create_obligation PR: duplicates at confirmation, a known paid period.

1. A preview must not create a twin: another channel may create the same name between the
   preview and «Да», and one request may carry the same name twice. The duplicate policy is
   stored in the confirmed plan and checked in the applying transaction.
2. «Купил 10 октября на месяц» gives the code a start and a length: it computes the end by
   calendar months. The price period alone proves nothing about what is paid.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.integration.test_create_obligation import (
    DEMO_TEXT,
    demo,
    obligations,
    pendings,
    plain,
    reply,
    say,
)
from xboct.application import commands as c
from xboct.storage.models import Obligation, Payment

# --- 1. duplicates at confirmation ---------------------------------------------------------


async def test_a_twin_created_after_the_preview_is_refused_at_yes_and_nothing_is_applied(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, DEMO_TEXT)
    assert preview.status == "preview"
    # The same subscription is created by another channel (a form) before «Да».
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1)))

    result = await say(x, "Да")
    assert result.status != "applied"
    assert result.code == "stale_preview"
    assert "ничего не применил" in result.message
    [only] = await obligations(x)
    assert only.coverage_until == date(2026, 12, 1)  # the form's record, no second one
    # The preview stays unresolved: nothing was applied, nothing is marked confirmed.
    assert len(await pendings(x)) == 1


async def test_a_twin_by_alias_or_extended_name_is_refused_at_yes_too(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    await x.ok(
        c.CreateObligation(
            title="Moonshot",
            aliases=["demo"],
            kind="coverage",
            coverage_until=date(2026, 12, 1),
        )
    )
    assert (await say(x, "Да")).code == "stale_preview"
    assert len(await obligations(x)) == 1


async def test_the_same_name_twice_in_one_request_is_asked_before_a_preview(x):
    both = {
        "kind": "actions",
        "actions": [demo()["actions"][0], demo()["actions"][0]],
    }
    x.provider.add("две Demo", both)
    result = await say(x, "две Demo")
    assert result.status == "clarification"
    assert result.code == "duplicate_in_request"
    assert "дважды «Demo»" in result.message
    assert await pendings(x) == []
    assert await obligations(x) == []


async def test_the_batch_check_also_catches_a_name_that_extends_the_other(x):
    both = {
        "kind": "actions",
        "actions": [
            demo()["actions"][0],
            demo(title="Demo API")["actions"][0],
        ],
    }
    x.provider.add("две", both)
    assert (await say(x, "две")).code == "duplicate_in_request"


async def test_the_applying_transaction_refuses_a_twin_inside_one_batch(x):
    """Even a plan stored without the batch check cannot create two records of one name."""
    first = c.CreateObligation(
        title="Demo", kind="coverage", coverage_until=date(2026, 11, 11), duplicates="forbid"
    )
    second = first.model_copy()
    result = await x.run(c.ConfirmPendingIntent(pending_id="missing"))
    assert result.status != "applied"  # sanity: a made-up id applies nothing
    from xboct.storage.models import PendingIntent

    pending = PendingIntent(
        command_id="manual",
        actions=[first.model_dump(mode="json"), second.model_dump(mode="json")],
        candidates=[],
        expected_versions={},
        summary="x",
        status="pending",
        created_at=x.clock.now(),
        expires_at=x.clock.now().replace(year=2030),
    )
    async with x.app.sessions() as session, session.begin():
        session.add(pending)
    result = await x.run(c.ConfirmPendingIntent(pending_id=pending.id))
    assert result.code == "stale_preview"
    assert await obligations(x) == []  # the first create was rolled back with the group


async def test_an_explicit_second_record_stays_possible_and_is_visible_in_the_preview(x):
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 11, 11)))
    x.provider.add("ещё одну Demo", demo(allow_duplicate=True))
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, "ещё одну Demo")
    assert preview.status == "preview"
    assert "«Demo» уже есть — по твоей просьбе создаю вторую запись" in preview.message
    [pending] = await pendings(x)
    assert pending.actions[0]["duplicates"] == "allow"
    done = await say(x, "Да")
    assert done.status == "applied"
    assert len(await obligations(x)) == 2


async def test_the_explicit_second_record_survives_a_new_twin_after_the_preview(x):
    x.provider.add("ещё Demo", demo(allow_duplicate=True))
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, "ещё Demo")
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1)))
    assert (await say(x, "Да")).status == "applied"
    assert len(await obligations(x)) == 2


async def test_a_mixed_batch_is_atomic_when_the_twin_appears_after_the_preview(x):
    mixed = {
        "kind": "actions",
        "actions": [
            {
                "type": "create_task",
                "title": "Оплатить штраф",
                "note": None,
                "url": None,
                "next_action": None,
                "recurrence": None,
                "due": {"date": {"kind": "tomorrow"}, "time": None},
            },
            demo()["actions"][0],
        ],
    }
    x.provider.add("штраф и Demo", mixed)
    x.provider.add("Да", reply("confirm_pending"))
    assert (await say(x, "штраф и Demo")).status == "preview"
    await x.ok(c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1)))
    result = await say(x, "Да")
    assert result.code == "stale_preview"
    from xboct.storage.models import Task

    assert await x.all(Task) == []  # the task before the conflicting create was not kept
    assert len(await obligations(x)) == 1


async def test_forms_keep_creating_same_named_records_as_before(x):
    for _ in range(2):
        await x.ok(
            c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 12, 1))
        )
    assert len(await obligations(x)) == 2


# --- 2. a known start and a paid length ----------------------------------------------------

PURCHASE = "Купил Demo 10 октября на месяц, 17 долларов, продлевается каждый месяц"


def period(start: str, count: int = 1, unit: str = "month") -> dict:
    return {
        "start": {"kind": "date", "value": start},
        "length": {"unit": unit, "count": count},
    }


def purchase(**over) -> dict:
    return demo(
        paid_through=None,
        paid_period=period("2026-10-10"),
        first_payment_on={"kind": "date", "value": "2026-10-10"},
        **over,
    )


async def test_start_and_length_give_the_next_due_date_without_asking(x):
    x.provider.add(PURCHASE, purchase())
    x.provider.add("Да", reply("confirm_pending"))
    preview = await say(x, PURCHASE)
    assert preview.status == "preview"  # no question about the end
    text = plain(preview)
    assert "оплачено по 9 ноября (пн) включительно" in text
    assert "следующая оплата с 10 ноября (вт)" in text
    paid = "Оплачено с 10 октября 2026 (сб) на 1 месяц, по 9 ноября 2026 (пн) включительно"
    assert paid in text
    assert "Цена: 17 $ в месяц" in text
    done = await say(x, "Да")
    assert done.status == "applied"
    [ob] = await obligations(x)
    assert ob.coverage_until == date(2026, 11, 10)  # the first unpaid day = the next due date
    assert ob.recurrence_id is None
    assert "Первая оплата: 10 октября 2026 (сб) (со слов владельца)" in ob.note
    assert await x.all(Payment) == []  # no invented payment
    assert {j.kind for j in await x.pending_jobs(ob.id)} >= {"advance", "due", "overdue"}


@pytest.mark.parametrize(
    ("start", "count", "unit", "first_unpaid"),
    [
        ("2026-10-31", 1, "month", date(2026, 11, 30)),  # month end clamps
        ("2026-12-10", 1, "month", date(2027, 1, 10)),  # year border
        ("2026-12-31", 2, "month", date(2027, 2, 28)),
        ("2027-01-29", 1, "month", date(2027, 2, 28)),
        ("2027-01-30", 1, "month", date(2027, 2, 28)),
        ("2028-01-31", 1, "month", date(2028, 2, 29)),  # leap year
        ("2026-10-10", 12, "month", date(2027, 10, 10)),
        ("2026-10-10", 1, "year", date(2027, 10, 10)),
        ("2026-10-10", 2, "week", date(2026, 10, 24)),
        ("2026-10-10", 10, "day", date(2026, 10, 20)),
    ],
)
async def test_calendar_arithmetic_is_done_by_the_code(x, start, count, unit, first_unpaid):
    x.provider.add("купил", demo(paid_through=None, paid_period=period(start, count, unit)))
    x.provider.add("Да", reply("confirm_pending"))
    # Starts after today are fine too: the facts are the owner's.
    await say(x, "купил")
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.coverage_until == first_unpaid


async def test_the_named_end_alone_is_kept_as_before(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, DEMO_TEXT)
    await say(x, "Да")
    [ob] = await obligations(x)
    assert ob.coverage_until == date(2026, 11, 11)
    assert "Оплачено с" not in (ob.note or "")


async def test_a_period_and_a_matching_end_agree(x):
    x.provider.add(
        "t",
        demo(
            paid_period=period("2026-10-10"),
            paid_through={"kind": "last_paid_day", "day": "2026-11-09"},
        ),
    )
    assert (await say(x, "t")).status == "preview"


async def test_contradicting_facts_get_one_specific_question(x):
    x.provider.add("t", demo(paid_period=period("2026-10-10")))  # the end 2026-11-10 disagrees
    result = await say(x, "t")
    assert result.status == "clarification"
    assert result.code == "paid_period_conflict"
    assert "с 10 октября (сб) на 1 месяц" in result.message
    assert "по 9 ноября (пн) включительно" in result.message
    assert "по 10 ноября (вт) включительно" in result.message
    assert await pendings(x) == []


async def test_the_price_period_alone_does_not_prove_coverage(x):
    x.provider.add("t", demo(paid_through=None, paid_period=None))
    result = await say(x, "t")
    assert result.code == "paid_through_missing"
    assert await pendings(x) == []


async def test_an_old_first_purchase_does_not_make_later_months_paid(x):
    x.provider.add(
        "t",
        demo(
            paid_through=None,
            first_payment_on={"kind": "date", "value": "2026-01-10"},
        ),
    )
    result = await say(x, "t")
    assert result.code == "paid_through_missing"
    assert "первая оплата была 10 января" in result.message
    assert await obligations(x) == []
    assert await pendings(x) == []


async def test_an_old_period_that_already_ended_is_asked_not_saved(x):
    x.provider.add("t", demo(paid_through=None, paid_period=period("2026-08-10")))
    result = await say(x, "t")
    assert result.code == "paid_period_elapsed"
    assert "закончилась 9 сентября (ср) включительно" in result.message
    assert await pendings(x) == []


async def test_an_impossible_length_is_a_question_not_a_crash(x):
    x.provider.add("t", demo(paid_through=None, paid_period=period("2026-10-10", 121)))
    result = await say(x, "t")
    assert result.code == "llm_invalid"  # the contract caps a paid length at 120
    assert await obligations(x) == []


async def test_a_period_for_a_calendar_payment_is_refused(x):
    rule = {"frequency": "monthly", "start": {"kind": "date", "value": "2026-11-05"}}
    x.provider.add(
        "t",
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_obligation",
                    "title": "Аренда",
                    "kind": "calendar",
                    "recurrence": rule,
                    "paid_period": period("2026-10-10"),
                }
            ],
        },
    )
    result = await say(x, "t")
    assert result.status != "preview"
    assert await pendings(x) == []


async def test_the_preview_style_stays_informal(x):
    from xboct.style import formal_markers

    x.provider.add(PURCHASE, purchase())
    assert formal_markers((await say(x, PURCHASE)).message) == []


async def test_nothing_is_created_for_the_existing_archived_demo(x):
    """The production Demo stays archived: a new text creation never touches it."""
    made = await x.ok(
        c.CreateObligation(title="Demo", kind="coverage", coverage_until=date(2026, 9, 1))
    )
    await x.ok(
        c.ArchiveObligation(
            obligation_id=made.data["obligation_id"], expected_version=1, outstanding="keep_open"
        )
    )
    x.provider.add(PURCHASE, purchase())
    x.provider.add("Да", reply("confirm_pending"))
    await say(x, PURCHASE)
    await say(x, "Да")
    rows = await x.all(Obligation)
    assert sorted(o.active for o in rows) == [False, True]
    archived = next(o for o in rows if not o.active)
    assert archived.coverage_until == date(2026, 9, 1)


def test_the_prompt_separates_a_paid_period_from_a_price_period():
    from xboct.llm import openai_provider

    text = " ".join(openai_provider.INSTRUCTIONS.split())
    assert "paid_period: when the owner states where the paid time STARTS" in text
    assert "Never compute the end yourself" in text
    assert "is NOT a paid period" in text
    assert "Never set paid_period for kind=calendar" in text


def test_the_strict_schema_carries_paid_period():
    from xboct.llm.contract import IntentEnvelope
    from xboct.llm.schema import check_strict_subset, strict_schema

    schema = strict_schema(IntentEnvelope)
    assert check_strict_subset(schema) == []
    assert "PaidPeriod" in str(schema)


# --- review 2: a question must not leave the older plan confirmable --------------------------


async def calls_of(x) -> int:
    return len(x.provider.calls)


async def test_an_unresolved_conflict_supersedes_the_old_plan_and_yes_applies_nothing(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("уточнение", demo(paid_period=period("2026-10-10")))  # contradicts the end
    assert (await say(x, DEMO_TEXT)).status == "preview"
    asked = await say(x, "уточнение")
    assert asked.code == "paid_period_conflict"
    # The old plan is no longer confirmable; the answer to the question is not «Да».
    assert await pendings(x) == []
    assert len(await pendings(x, "superseded")) == 1
    before = await calls_of(x)
    result = await say(x, "Да")
    assert result.status == "clarification"
    assert result.code == "no_current_pending"
    assert "заменено" in result.message
    assert await obligations(x) == []
    assert await calls_of(x) == before  # decided by the code


async def test_the_button_of_the_superseded_plan_does_nothing_either(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("уточнение", demo(paid_period=period("2026-10-10")))
    await say(x, DEMO_TEXT)
    [old] = await pendings(x)
    await say(x, "уточнение")
    result = await x.run(c.ConfirmPendingIntent(pending_id=old.id))
    assert result.code == "pending_superseded"
    assert await obligations(x) == []


async def test_a_model_clarification_also_retires_the_old_plan(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add(
        "а ещё Midjourney",
        {"kind": "clarification", "clarification": {"question": "Сколько стоит Midjourney?"}},
    )
    await say(x, DEMO_TEXT)
    assert (await say(x, "а ещё Midjourney")).status == "clarification"
    assert await pendings(x) == []
    assert (await say(x, "Да")).status == "clarification"
    assert await obligations(x) == []


async def test_an_information_query_does_not_rewrite_the_plan(x):
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("что сегодня?", {"kind": "query", "query": {"kind": "agenda"}})
    x.provider.add("привет", {"kind": "conversation", "conversation_kind": "greeting"})
    await say(x, DEMO_TEXT)
    assert (await say(x, "что сегодня?")).status == "answered"
    assert (await say(x, "привет")).status == "answered"
    assert len(await pendings(x)) == 1
    assert (await say(x, "Да")).status == "applied"
    assert len(await obligations(x)) == 1


async def test_a_rejected_invalid_proposal_keeps_the_plan(x):
    """Only a question retires the plan; a refusal changes nothing."""
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add("битый", demo(recurrence={"frequency": "monthly", "start": {"kind": "today"}}))
    await say(x, DEMO_TEXT)
    assert (await say(x, "битый")).status == "rejected"
    assert len(await pendings(x)) == 1


# --- review 2: bare «да»/«нет» are decided by the code from the stored plan ------------------


@pytest.mark.parametrize("word", ["Да", "да!", " ДА. ", "Да, подтверждаю", "подтверждаю", "Верно"])
async def test_bare_yes_confirms_the_current_plan_without_a_model_call(x, word):
    x.provider.add(DEMO_TEXT, demo())
    await say(x, DEMO_TEXT)
    before = await calls_of(x)
    result = await say(x, word)
    assert result.status == "applied"
    assert await calls_of(x) == before
    assert len(await obligations(x)) == 1


@pytest.mark.parametrize("word", ["Нет", "не надо", "Отмена!", "отмени"])
async def test_bare_no_cancels_the_current_plan_without_a_model_call(x, word):
    x.provider.add(DEMO_TEXT, demo())
    await say(x, DEMO_TEXT)
    before = await calls_of(x)
    result = await say(x, word)
    assert result.message == "Ок, ничего не меняю."
    assert await calls_of(x) == before
    assert await obligations(x) == []


async def test_each_stored_state_gets_its_own_honest_answer(x):
    absent = await say(x, "Да")
    assert absent.message == "Сохранённого предложения нет. Напиши, что добавить."

    x.provider.add(DEMO_TEXT, demo())
    await say(x, DEMO_TEXT)
    x.clock.advance(timedelta(minutes=31))
    expired = await say(x, "Да")
    assert expired.message == "Предложение устарело. Напиши, что добавить, ещё раз."

    x.provider.add("снова", demo())
    await say(x, "снова")
    await say(x, "Нет")
    cancelled = await say(x, "Да")
    assert cancelled.message == "Предложение уже отменено. Напиши, что добавить, если нужно."

    x.provider.add("ещё", demo())
    await say(x, "ещё")
    x.provider.add("ещё раз", demo(title="Midjourney"))
    await say(x, "ещё раз")  # replaces the previous preview
    assert len(await pendings(x, "superseded")) >= 1
    await say(x, "Да")  # confirms Midjourney
    done = await say(x, "Да")
    assert done.message == "Это предложение уже применено. Напиши, что добавить."
    assert [o.title for o in await obligations(x)] == ["Midjourney"]


@pytest.mark.parametrize(
    "phrase", ["ок", "давай", "ага, спасибо", "да я не уверен", "нет, лучше в доллары", "может да"]
)
async def test_chatter_is_not_a_confirmation(x, phrase):
    from xboct.application.intents import short_answer

    assert short_answer(phrase) is None
    x.provider.add(DEMO_TEXT, demo())
    x.provider.add(phrase, {"kind": "conversation", "conversation_kind": "acknowledgement"})
    await say(x, DEMO_TEXT)
    assert (await say(x, phrase)).status == "answered"
    assert await obligations(x) == []
    assert len(await pendings(x)) == 1


async def test_a_redelivered_bare_yes_is_a_replay(x):
    x.provider.add(DEMO_TEXT, demo())
    await say(x, DEMO_TEXT)
    first = await say(x, "Да", command_id="tg-update:700")
    again = await say(x, "Да", command_id="tg-update:700")
    assert first.status == "applied"
    assert again.replayed
    assert len(await obligations(x)) == 1


# --- review 2: eval honesty -------------------------------------------------------------------


async def test_the_eval_marks_a_code_decided_step_and_fails_if_the_model_was_called(tmp_path):
    import copy

    from tests.integration.test_evals import case
    from xboct.llm.evals import IdealProvider, run_case

    source = case("yes-without-preview")
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert result.passed, result.failures
    assert result.diagnostics["route"] == "code"
    # The same case claiming the model must answer fails: the route is checked, not assumed.
    wrong = copy.deepcopy(source)
    wrong["expect"]["route"] = "model"
    wrong["ideal"] = {"kind": "confirm_pending"}
    failed = await run_case(wrong, IdealProvider(wrong), tmp_path)
    assert any("model was not called" in f for f in failed.failures)


async def test_a_derived_title_may_differ_in_case_an_explicit_one_may_not(tmp_path):
    import copy

    from tests.integration.test_evals import case
    from xboct.llm.evals import IdealProvider, run_case

    source = copy.deepcopy(case("create-calendar-rent"))
    source["steps"][0]["ideal"]["actions"][0]["title"] = "аренда"  # as the owner wrote it
    ok = await run_case(source, IdealProvider(source), tmp_path)
    assert ok.passed, ok.failures
    # The explicit-title check stays exact.
    strict = copy.deepcopy(case("demo-create-clear"))
    strict["steps"][0]["ideal"]["actions"][0]["title"] = "demo"
    bad = await run_case(strict, IdealProvider(strict), tmp_path)
    assert not bad.passed


def test_the_prompt_keeps_a_named_end_and_forbids_a_second_one_from_the_tariff():
    from xboct.llm import openai_provider

    text = " ".join(openai_provider.INSTRUCTIONS.split())
    assert "paid_period needs an explicit paid duration" in text
    assert "never make a paid_period by themselves" in text
    assert "keep that paid_through in the full proposal" in text
    assert "do not derive a second, conflicting end from the tariff" in text
    assert openai_provider.PROMPT_VERSION == "intent-v8-paid-period-rules"
