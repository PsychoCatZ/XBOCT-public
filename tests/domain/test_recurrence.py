from datetime import date

import pytest

from xboct.domain.coverage import extend_coverage, paid_through
from xboct.domain.errors import ValidationFailed
from xboct.domain.recurrence import RecurrenceRule


def dates(rule: RecurrenceRule, start: date, end: date) -> list[date]:
    return list(rule.dates_between(start, end))


def test_month_end_anchor_is_kept():
    # A07: 31 January -> last day of February -> 31 March.
    rule = RecurrenceRule("monthly", date(2027, 1, 31))
    assert dates(rule, date(2027, 1, 1), date(2027, 5, 31)) == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
        date(2027, 4, 30),
        date(2027, 5, 31),
    ]
    leap = RecurrenceRule("monthly", date(2028, 1, 31))
    assert dates(leap, date(2028, 2, 1), date(2028, 2, 29)) == [date(2028, 2, 29)]


def test_february_29_yearly():
    rule = RecurrenceRule("yearly", date(2028, 2, 29))
    assert dates(rule, date(2028, 1, 1), date(2032, 12, 31)) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]


def test_every_three_months_is_calendar_not_90_days():
    rule = RecurrenceRule("monthly", date(2026, 11, 30), interval=3)
    assert dates(rule, date(2026, 1, 1), date(2027, 12, 31)) == [
        date(2026, 11, 30),
        date(2027, 2, 28),
        date(2027, 5, 30),
        date(2027, 8, 30),
        date(2027, 11, 30),
    ]


def test_window_start_does_not_change_sequence():
    rule = RecurrenceRule("monthly", date(2027, 1, 31))
    assert dates(rule, date(2027, 3, 1), date(2027, 3, 31)) == [date(2027, 3, 31)]


def test_daily_weekly():
    daily = RecurrenceRule("daily", date(2026, 10, 8), interval=2)
    assert dates(daily, date(2026, 10, 9), date(2026, 10, 14)) == [
        date(2026, 10, 10),
        date(2026, 10, 12),
        date(2026, 10, 14),
    ]
    weekly = RecurrenceRule("weekly", date(2026, 10, 8), weekdays=(0, 3))  # Mon, Thu
    assert dates(weekly, date(2026, 10, 1), date(2026, 10, 19)) == [
        date(2026, 10, 8),
        date(2026, 10, 12),
        date(2026, 10, 15),
        date(2026, 10, 19),
    ]


def test_period_keys():
    assert (
        RecurrenceRule("monthly", date(2026, 10, 15)).period_key(date(2026, 11, 15)) == "M:2026-11"
    )
    assert RecurrenceRule("yearly", date(2026, 10, 15)).period_key(date(2027, 10, 15)) == "Y:2027"
    assert RecurrenceRule("daily", date(2026, 10, 15)).period_key(date(2026, 10, 16)) == (
        "D:2026-10-16"
    )


def test_invalid_rules_rejected():
    with pytest.raises(ValidationFailed):
        RecurrenceRule("monthly", date(2026, 1, 1), interval=0)
    with pytest.raises(ValidationFailed):
        RecurrenceRule("daily", date(2026, 1, 1), weekdays=(1,))


def test_coverage_three_calendar_months():
    # A08: exclusive coverage date moves by calendar months.
    until = extend_coverage(date(2026, 10, 15), "month", 3)
    assert until == date(2027, 1, 15)
    assert paid_through(until) == date(2027, 1, 14)
    assert extend_coverage(date(2026, 11, 30), "month", 3) == date(2027, 2, 28)
    assert extend_coverage(date(2026, 10, 15), "year", 1) == date(2027, 10, 15)
