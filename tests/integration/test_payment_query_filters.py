from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from xboct.application import commands as c
from xboct.application.payment_queries import period_bounds
from xboct.llm.contract import QuerySpec
from xboct.storage.models import Obligation, Occurrence, Payment


async def subscriptions(x):
    for title, first_unpaid in (
        ("ChatGPT", date(2026, 11, 9)),
        ("Claude", date(2026, 10, 16)),
        ("Gemini", date(2026, 10, 29)),
    ):
        await x.ok(c.CreateObligation(title=title, kind="coverage", coverage_until=first_unpaid))


@pytest.mark.parametrize(
    "phrase",
    [
        "Что у меня по оплате на этот месяц?",
        "Что мне надо оплатить в этом месяце?",
        "Что мне осталось оплатить в этом месяце?",
        "Что я ещё не оплатил в этом месяце?",
    ],
)
async def test_october_questions_keep_period_and_unpaid_status(x, phrase):
    await subscriptions(x)
    before = [(o.id, o.coverage_until, o.version) for o in await x.all(Obligation)]
    x.provider.add(
        phrase,
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "period": "this_month",
                "payment_status": "unpaid",
            },
        },
    )
    result = await x.say(phrase)
    assert result.status == "answered"
    assert "01.10.2026" in result.message
    assert "31.10.2026" in result.message
    assert "Claude" in result.message
    assert "15 октября" in result.message
    assert "Gemini" in result.message
    assert "28 октября" in result.message
    assert "ChatGPT" not in result.message
    assert "продлить" in result.message
    assert before == [(o.id, o.coverage_until, o.version) for o in await x.all(Obligation)]
    assert await x.all(Payment) == []


async def test_next_month_uses_coverage_deadline_not_payment_date(x):
    await subscriptions(x)
    x.provider.add(
        "ноябрь",
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "period": "next_month",
                "payment_status": "unpaid",
            },
        },
    )
    result = await x.say("ноябрь")
    assert "ChatGPT" in result.message
    assert "8 ноября" in result.message
    assert "Claude" not in result.message
    assert "Gemini" not in result.message


async def test_paid_history_uses_actual_paid_at_not_coverage_deadline(x):
    created = await x.ok(c.CreateObligation(title="ChatGPT", kind="coverage"))
    oid = created.data["obligation_id"]
    ob = await x.get(Obligation, oid)
    await x.ok(
        c.ExtendCoverage(
            obligation_id=oid,
            expected_version=ob.version,
            start=date(2026, 10, 8),
            until=date(2026, 11, 9),
        )
    )
    x.provider.add(
        "оплатил",
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "period": "this_month",
                "payment_status": "paid",
            },
        },
    )
    result = await x.say("оплатил")
    assert "ChatGPT" in result.message
    assert "8 октября" in result.message
    assert "8 ноября" in result.message
    assert "История оплат" in result.message


async def test_paid_calendar_period_is_excluded_from_unpaid_query(x):
    created = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    oid = created.data["obligation_id"]
    october = next(
        o for o in await x.all(Occurrence) if o.owner_id == oid and o.period_key == "M:2026-10"
    )
    await x.ok(
        c.MarkPayment(
            obligation_id=oid,
            occurrences=[c.OccurrenceRef(id=october.id, expected_version=october.version)],
        )
    )
    x.provider.add(
        "остаток",
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "period": "this_month",
                "payment_status": "unpaid",
            },
        },
    )
    result = await x.say("остаток")
    assert "VPS" not in result.message
    assert "с таким сроком нет" in result.message


def test_query_period_boundaries_are_calendar_based():
    today = date(2026, 10, 8)
    assert period_bounds(QuerySpec(kind="payments", period="next_week"), today) == (
        date(2026, 10, 12),
        date(2026, 10, 18),
    )
    assert period_bounds(QuerySpec(kind="payments", period="next_month"), date(2026, 12, 31)) == (
        date(2027, 1, 1),
        date(2027, 1, 31),
    )
    assert period_bounds(QuerySpec(kind="payments", period="month", month=2, year=2028), today) == (
        date(2028, 2, 1),
        date(2028, 2, 29),
    )


def test_reversed_or_incomplete_explicit_period_is_rejected():
    with pytest.raises(ValidationError):
        QuerySpec(
            kind="payments",
            period="range",
            start_date=date(2026, 10, 31),
            end_date=date(2026, 10, 1),
        )
    with pytest.raises(ValidationError):
        QuerySpec(kind="payments", period="month")


async def test_current_month_is_resolved_from_received_date(x):
    from xboct.application.context import CommandContext

    await subscriptions(x)
    received = x.clock.now().replace(day=31)
    x.clock.set(received + timedelta(days=1))
    x.provider.add(
        "месяц",
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "period": "this_month",
                "payment_status": "unpaid",
            },
        },
    )
    result = await x.intents.handle_message(
        "месяц", CommandContext("delayed-query", "test", received)
    )
    assert "31.10.2026" in result.message
    assert "Claude" in result.message
    assert "ChatGPT" not in result.message
