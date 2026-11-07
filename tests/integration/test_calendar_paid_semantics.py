"""«Оплачено сейчас» for calendar payments (decision of the architect, after PR #7).

A calendar payment shows the confirmed fact «оплата за <период> отмечена» (nominal and
next due). It is never turned into coverage: for a quarterly or yearly payment the gap
between two nominal dates is not proof of how long the payment covers. Only a stored
coverage_until (a coverage obligation) is shown as «оплачено по <день> включительно».
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.integration.test_payment_overdue_block import ask
from xboct.application import commands as c
from xboct.storage.models import Occurrence


async def pay(x, obligation_id: str, period_key: str) -> None:
    [occ] = await x.all(
        Occurrence, Occurrence.owner_id == obligation_id, Occurrence.period_key == period_key
    )
    await x.ok(
        c.MarkPayment(
            obligation_id=obligation_id,
            occurrences=[c.OccurrenceRef(id=occ.id, expected_version=occ.version)],
        )
    )


async def calendar(x, title: str, frequency: str, anchor: date, interval: int = 1) -> str:
    result = await x.ok(
        c.CreateObligation(
            title=title,
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency=frequency, anchor_date=anchor, interval=interval),
        )
    )
    return result.data["obligation_id"]


@pytest.mark.parametrize(
    ("frequency", "interval", "anchor", "period_key", "paid_period", "next_due"),
    [
        ("monthly", 3, date(2026, 7, 15), "M:2026-10", "октябрь 2026", "15 января 2027"),
        ("yearly", 1, date(2026, 3, 1), "Y:2026", "2026 год", "1 марта 2027"),
    ],
)
async def test_quarterly_and_yearly_marks_are_not_turned_into_coverage(
    x, frequency, interval, anchor, period_key, paid_period, next_due
):
    ob_id = await calendar(x, "Налог", frequency, anchor, interval)
    await pay(x, ob_id, period_key)
    text = await ask(x, period="all", payment_status="covered")
    assert "Отмеченные календарные оплаты:" in text
    assert "«Налог»" not in text  # title is printed without quotes in this block
    assert f"Налог — оплата за {paid_period} отмечена" in text
    assert next_due in text  # the next due is shown as a due date only
    # Never a coverage claim derived from the nominal dates.
    assert "оплачено по" not in text
    assert "включительно" not in text
    assert "Сейчас оплачено:" not in text  # that heading belongs to coverage obligations
    assert "срок покрытия по ним не вычисляется" in text


async def test_no_day_before_next_due_is_invented(x):
    # Quarterly from 15 Jul: Oct paid, next due 15 Jan. «Paid through 14 Jan» is exactly
    # the invented coverage this decision forbids.
    ob_id = await calendar(x, "Налог", "monthly", date(2026, 7, 15), interval=3)
    await pay(x, ob_id, "M:2026-10")
    for query in (
        {"period": "all", "payment_status": "covered"},
        {"period": "this_month", "payment_status": "covered"},
    ):
        text = await ask(x, **query)
        assert "14 января" not in text
        assert "15 января 2027" in text or "подходящих записей нет" in text


async def test_coverage_and_calendar_facts_are_separate_blocks(x):
    await x.ok(c.CreateObligation(title="Домен", kind="coverage", coverage_until=date(2026, 11, 9)))
    ob_id = await calendar(x, "Аренда", "monthly", date(2026, 9, 5))
    await pay(x, ob_id, "M:2026-09")
    text = await ask(x, period="all", payment_status="covered")
    coverage, _, marks = text.partition("\n\n")
    assert coverage.startswith("Сейчас оплачено:")
    assert "Домен — оплачено по 8 ноября (вс) включительно." in coverage
    assert "Аренда" not in coverage
    assert marks.startswith("Отмеченные календарные оплаты:")
    assert "Аренда — оплата за сентябрь 2026 отмечена (срок 5 сентября (сб))" in marks
    assert "Домен" not in marks
    assert "оплачено по" not in marks


async def test_unpaid_calendar_is_not_listed_as_marked(x):
    await calendar(x, "Аренда", "monthly", date(2026, 9, 5))
    text = await ask(x, period="all", payment_status="covered")
    assert text == "Сейчас оплачено: подходящих записей нет."


async def test_payment_history_is_kept(x):
    ob_id = await calendar(x, "Налог", "monthly", date(2026, 7, 15), interval=3)
    await pay(x, ob_id, "M:2026-10")
    history = await ask(x, period="all", payment_status="paid")
    assert "История оплат" in history
    assert "«Налог»" in history
    assert "оплачено за октябрь 2026" in history


async def test_window_question_lists_only_marks_inside_the_window(x):
    ob_id = await calendar(x, "Аренда", "monthly", date(2026, 8, 5))
    await pay(x, ob_id, "M:2026-08")
    await pay(x, ob_id, "M:2026-09")
    text = await ask(x, period="this_month", payment_status="covered")
    assert "подходящих записей нет" in text or "октябрь" in text
    assert "август" not in text
