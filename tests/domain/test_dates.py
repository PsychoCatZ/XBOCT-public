from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from xboct.domain.dates import (
    DayOfMonth,
    EndOfNextWeek,
    EveningSlot,
    ExactTime,
    InDays,
    Later,
    NearestWeekday,
    NextWeek,
    ResolveContext,
    Today,
    Tomorrow,
    When,
    WithinWeek,
    next_week_range,
    resolve_date,
    resolve_event_instant,
    resolve_reminder_instant,
    resolve_when,
)
from xboct.domain.errors import NeedsClarification, ValidationFailed

KRSK = ZoneInfo("Asia/Krasnoyarsk")
THURSDAY = date(2026, 10, 8)


def ctx(hour: int = 10, minute: int = 0, day: date = THURSDAY) -> ResolveContext:
    return ResolveContext(
        received_at=datetime(day.year, day.month, day.day, hour, minute, tzinfo=KRSK), tz=KRSK
    )


def test_within_week_is_seven_calendar_days():
    # A01: 8 October «в течение недели» -> 15 October.
    assert resolve_date(WithinWeek(), THURSDAY) == date(2026, 10, 15)


def test_relative_simple_dates():
    assert resolve_date(Today(), THURSDAY) == THURSDAY
    assert resolve_date(Tomorrow(), THURSDAY) == date(2026, 10, 9)
    assert resolve_date(InDays(days=3), THURSDAY) == date(2026, 10, 11)


def test_next_week_without_day_needs_clarification():
    with pytest.raises(NeedsClarification) as exc:
        resolve_date(NextWeek(), THURSDAY)
    assert exc.value.code == "next_week_needs_day"
    assert exc.value.options[0] == {"date": "2026-10-12"}
    assert next_week_range(THURSDAY) == (date(2026, 10, 12), date(2026, 10, 18))
    assert next_week_range(date(2026, 10, 12)) == (date(2026, 10, 19), date(2026, 10, 25))


def test_end_of_next_week_is_its_sunday():
    assert resolve_date(EndOfNextWeek(), THURSDAY) == date(2026, 10, 18)
    assert resolve_date(EndOfNextWeek(), date(2026, 10, 12)) == date(2026, 10, 25)


def test_nearest_weekday_includes_today():
    assert resolve_date(NearestWeekday(weekday=3), THURSDAY) == THURSDAY
    assert resolve_date(NearestWeekday(weekday=4), THURSDAY) == date(2026, 10, 9)
    assert resolve_date(NearestWeekday(weekday=2), THURSDAY) == date(2026, 10, 14)


def test_day_of_month_nearest_including_today():
    assert resolve_date(DayOfMonth(day=15), THURSDAY) == date(2026, 10, 15)
    assert resolve_date(DayOfMonth(day=8), THURSDAY) == THURSDAY
    assert resolve_date(DayOfMonth(day=5), THURSDAY) == date(2026, 11, 5)
    assert resolve_date(DayOfMonth(day=31), date(2026, 11, 3)) == date(2026, 11, 30)


def test_time_already_passed_today_asks_instead_of_shifting_a_week():
    with pytest.raises(NeedsClarification) as exc:
        resolve_when(
            When(date=NearestWeekday(weekday=3), time=ExactTime(value=time(9, 0))), ctx(10)
        )
    assert exc.value.code == "time_passed"


def test_evening_is_19_and_date_only_has_no_time():
    assert resolve_when(When(date=Today(), time=EveningSlot()), ctx(10)) == (
        THURSDAY,
        time(19, 0),
    )
    # A04: «завтра» without a time -> explicit date, no invented time.
    assert resolve_when(When(date=Tomorrow()), ctx(10)) == (date(2026, 10, 9), None)


def test_relative_dates_use_message_time_not_processing_time():
    # Message sent 23:50 on the 8th; a slow model must not turn «завтра» into the 10th.
    late = ctx(23, 50)
    assert resolve_when(When(date=Tomorrow()), late) == (date(2026, 10, 9), None)


def test_reminder_instant_for_snooze_and_later():
    assert resolve_reminder_instant(When(date=Today(), time=EveningSlot()), ctx(10)) == datetime(
        2026, 10, 8, 19, 0, tzinfo=KRSK
    )
    assert resolve_reminder_instant(Later(), ctx(10)) == datetime(2026, 10, 8, 12, 0, tzinfo=KRSK)
    with pytest.raises(NeedsClarification):
        resolve_reminder_instant(When(date=Today(), time=EveningSlot()), ctx(20))


def test_event_instant_past_only():
    assert resolve_event_instant(None, None, ctx(10)) == datetime(2026, 10, 8, 10, 0, tzinfo=KRSK)
    yesterday = resolve_event_instant(InDays(days=0), None, ctx(10))
    assert yesterday == datetime(2026, 10, 8, 10, 0, tzinfo=KRSK)
    with pytest.raises(ValidationFailed):
        resolve_event_instant(Tomorrow(), None, ctx(10))
