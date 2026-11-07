"""Review of PR #6: past-due items, a month named without a year, balance states.

A new period does not close an old one: an unpaid question about this month still shows
what is past due, in its own block, exactly once. Wording never claims that a service is
off or that nothing was paid — only that no payment is marked.
"""

from __future__ import annotations

from datetime import date

import pytest

from xboct.application import commands as c
from xboct.application.payment_queries import payment_query_text, period_bounds
from xboct.llm.contract import QuerySpec


async def ask(x, **query) -> str:
    spec = QuerySpec(kind="payments", **query)
    return await x.app.read(lambda uow: payment_query_text(uow, spec))


async def setup_records(x) -> dict[str, str]:
    ids = {}
    # Monthly rent from 5 Sep: September is past due, October is past due (5 Oct < 8 Oct).
    ids["rent"] = (
        await x.ok(
            c.CreateObligation(
                title="Аренда",
                kind="calendar",
                recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 9, 5)),
            )
        )
    ).data["obligation_id"]
    # Paid through 19 Sep: the paid period has ended.
    await x.ok(c.CreateObligation(title="Домен", kind="coverage", coverage_until=date(2026, 9, 20)))
    # Paid through 25 Oct: an upcoming renewal inside October.
    await x.ok(
        c.CreateObligation(title="Подписка", kind="coverage", coverage_until=date(2026, 10, 26))
    )
    # Monthly hosting from 20 Sep, September paid: only October (20 Oct) is upcoming.
    hosting = await x.ok(
        c.CreateObligation(
            title="Хостинг",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 9, 20)),
        )
    )
    ids["hosting"] = hosting.data["obligation_id"]
    return ids


async def pay_period(x, obligation_id: str, period_key: str) -> None:
    from xboct.storage.models import Occurrence

    [occ] = await x.all(
        Occurrence, Occurrence.owner_id == obligation_id, Occurrence.period_key == period_key
    )
    await x.ok(
        c.MarkPayment(
            obligation_id=obligation_id,
            occurrences=[c.OccurrenceRef(id=occ.id, expected_version=occ.version)],
        )
    )


async def test_this_month_unpaid_keeps_old_debts_in_their_own_block(x):
    ids = await setup_records(x)
    await pay_period(x, ids["hosting"], "M:2026-09")
    text = await ask(x, period="this_month", payment_status="unpaid")
    main, _, overdue = text.partition("Срок прошёл, отметки оплаты нет:")
    assert main.startswith("Ещё потребуется оплатить с 01.10.2026 по 31.10.2026:")
    # Upcoming inside October only.
    assert "«Подписка»" in main
    assert "«Хостинг» за октябрь 2026" in main
    # Past due, whatever the window: the September rent and the ended domain too.
    assert "«Аренда» за сентябрь 2026" in overdue
    assert "«Аренда» за октябрь 2026" in overdue
    assert "«Домен»: последний оплаченный день — 19 сентября" in overdue
    # Each item exactly once; paid periods are not past due.
    assert text.count("«Аренда» за октябрь 2026") == 1
    assert "«Аренда»" not in main
    assert "Хостинг» за сентябрь" not in text
    assert "отключ" not in text
    assert "не оплачен" not in text  # not marked is not proof of non-payment


async def test_next_month_unpaid_still_lists_past_due(x):
    await setup_records(x)
    text = await ask(x, period="next_month", payment_status="unpaid")
    main, _, overdue = text.partition("Срок прошёл, отметки оплаты нет:")
    assert "с 01.11.2026 по 30.11.2026" in main
    assert "«Аренда» за ноябрь 2026" in main
    assert "«Аренда» за сентябрь 2026" in overdue
    assert "«Домен»" in overdue


async def test_cancelled_and_archived_are_not_past_due(x):
    ids = await setup_records(x)
    await x.ok(
        c.ArchiveObligation(obligation_id=ids["rent"], expected_version=1, outstanding="keep_open")
    )
    from xboct.storage.models import Occurrence

    hosting_sept = (
        await x.all(
            Occurrence,
            Occurrence.owner_id == ids["hosting"],
            Occurrence.period_key == "M:2026-09",
        )
    )[0]
    await x.ok(c.SkipOccurrence(occurrence_id=hosting_sept.id, expected_version=1))
    text = await ask(x, period="this_month", payment_status="unpaid")
    assert "Аренда" not in text  # archived (its open periods stay in history only)
    assert "Хостинг» за сентябрь" not in text  # cancelled (skipped) period


async def test_without_past_due_there_is_no_extra_block(x):
    await x.ok(
        c.CreateObligation(title="Подписка", kind="coverage", coverage_until=date(2026, 10, 26))
    )
    text = await ask(x, period="this_month", payment_status="unpaid")
    assert "Срок прошёл" not in text
    assert "«Подписка»" in text


async def test_all_status_window_is_unchanged(x):
    await setup_records(x)
    text = await ask(x, period="this_month", payment_status="all")
    assert "Срок прошёл, отметки оплаты нет:" not in text  # no separate block
    assert "«Аренда» за сентябрь" not in text  # outside the window


# --- balance states -----------------------------------------------------------------


async def balance(x, title: str, *, rate: int, topup: int) -> None:
    result = await x.ok(
        c.CreateObligation(
            title=title,
            kind="balance",
            balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=rate),
        )
    )
    if topup:
        await x.ok(c.RecordTopup(obligation_id=result.data["obligation_id"], amount_minor=topup))


async def test_zero_rate_is_stopped_not_unknown(x):
    await balance(x, "Старый VPS", rate=0, topup=50_000)
    text = await ask(x, period="this_month", payment_status="unpaid")
    assert "Списание остановлено (расход 0): Старый VPS." in text
    assert "Не могу определить" not in text


async def test_depleted_estimate_is_past_due_not_paid(x):
    await balance(x, "VPS", rate=4_000, topup=8_000)  # two days
    x.clock.advance(days=5)
    text = await ask(x, period="next_month", payment_status="unpaid")
    main, _, overdue = text.partition("Срок прошёл, отметки оплаты нет:")
    assert "VPS" not in main
    assert "«VPS»: расчётный запас исчерпан — пора пополнить" in overdue
    assert "оценка по расходу, не данные провайдера" in overdue
    assert "отключ" not in text


async def test_balance_without_baseline_is_unknown(x):
    await x.ok(c.CreateObligation(title="Новый VPS", kind="balance"))
    text = await ask(x, period="this_month", payment_status="unpaid")
    assert "Не могу определить срок для: Новый VPS." in text


# --- a month named without a year ---------------------------------------------------


@pytest.mark.parametrize(
    ("today", "month", "status", "year"),
    [
        (date(2026, 12, 10), 1, "unpaid", 2027),  # upcoming: the next January
        (date(2026, 12, 10), 12, "unpaid", 2026),  # the current month counts
        (date(2026, 12, 10), 1, "all", 2027),
        (date(2027, 1, 10), 12, "paid", 2026),  # history: the last December
        (date(2027, 1, 10), 1, "paid", 2027),  # the current month counts
        (date(2027, 1, 10), 12, "unpaid", 2027),  # upcoming December, not the past one
        (date(2026, 10, 8), 1, "covered", 2026),  # covered does not guess
    ],
)
def test_month_without_year(today, month, status, year):
    query = QuerySpec(kind="payments", period="month", month=month, payment_status=status)
    start, end = period_bounds(query, today)
    assert (start.year, start.month) == (year, month)
    assert end.month == month


def test_explicit_year_is_kept():
    query = QuerySpec(kind="payments", period="month", month=1, year=2026, payment_status="unpaid")
    assert period_bounds(query, date(2026, 12, 10))[0] == date(2026, 1, 1)


async def test_january_question_in_october_looks_ahead(x):
    await x.ok(
        c.CreateObligation(
            title="Налог",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="yearly", anchor_date=date(2027, 1, 15)),
        )
    )
    text = await ask(x, period="month", month=1, payment_status="unpaid")
    assert "с 01.01.2027 по 31.01.2027" in text
    assert "«Налог»" in text
