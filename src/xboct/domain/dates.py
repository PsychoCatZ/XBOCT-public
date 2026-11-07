"""Typed relative date/time expressions and their deterministic resolution.

A model (or a form) never supplies a computed timestamp. It supplies an expression;
the code resolves it against the message receive time in the owner's timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from xboct.domain.errors import NeedsClarification, ValidationFailed
from xboct.domain.timeutil import add_months, last_day_of_month, local_to_utc, to_local


class _Expr(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExplicitDate(_Expr):
    kind: Literal["date"] = "date"
    value: date


class Today(_Expr):
    kind: Literal["today"] = "today"


class Tomorrow(_Expr):
    kind: Literal["tomorrow"] = "tomorrow"


class InDays(_Expr):
    kind: Literal["in_days"] = "in_days"
    days: int = Field(ge=0, le=3660)


class InWeeks(_Expr):
    kind: Literal["in_weeks"] = "in_weeks"
    weeks: int = Field(ge=1, le=520)


class InMonths(_Expr):
    kind: Literal["in_months"] = "in_months"
    months: int = Field(ge=1, le=120)


class WithinWeek(_Expr):
    """«В течение недели» -> 7 calendar days from the message date."""

    kind: Literal["within_week"] = "within_week"


class NextWeek(_Expr):
    """«На следующей неделе» without a day: a range (next Monday..Sunday).

    As a deadline or reminder it is ambiguous and needs a clarification; Sunday is never
    chosen silently. Use EndOfNextWeek for an explicit «до конца следующей недели».
    """

    kind: Literal["next_week"] = "next_week"


class EndOfNextWeek(_Expr):
    """«До конца следующей недели» -> Sunday of next week."""

    kind: Literal["end_of_next_week"] = "end_of_next_week"


class NearestWeekday(_Expr):
    """«На пятницу» -> nearest such weekday, today included (0=Monday)."""

    kind: Literal["weekday"] = "weekday"
    weekday: int = Field(ge=0, le=6)


class DayOfMonth(_Expr):
    """«15-го» -> nearest date with that day of month, today included."""

    kind: Literal["day_of_month"] = "day_of_month"
    day: int = Field(ge=1, le=31)


DateExpr = Annotated[
    ExplicitDate
    | Today
    | Tomorrow
    | InDays
    | InWeeks
    | InMonths
    | WithinWeek
    | NextWeek
    | EndOfNextWeek
    | NearestWeekday
    | DayOfMonth,
    Field(discriminator="kind"),
]


class ExactTime(_Expr):
    kind: Literal["time"] = "time"
    value: time


class EveningSlot(_Expr):
    """«Вечером» -> configurable slot (default 19:00)."""

    kind: Literal["evening"] = "evening"


TimeExpr = Annotated[ExactTime | EveningSlot, Field(discriminator="kind")]


class When(_Expr):
    date: DateExpr
    time: TimeExpr | None = None


class Later(_Expr):
    """«Позже» without a time -> default delay (settings, 120 minutes)."""

    kind: Literal["later"] = "later"
    minutes: int | None = Field(default=None, ge=5, le=7 * 24 * 60)


@dataclass(frozen=True)
class ResolveContext:
    received_at: datetime
    tz: ZoneInfo
    evening: time = time(19, 0)
    notice_time: time = time(10, 0)
    later_minutes: int = 120

    @property
    def local_now(self) -> datetime:
        return to_local(self.received_at, self.tz)

    @property
    def today(self) -> date:
        return self.local_now.date()


def next_week_range(today: date) -> tuple[date, date]:
    """Next Monday..Sunday (for queries «что на следующей неделе»)."""
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=6)


def resolve_date(expr: DateExpr, today: date) -> date:
    match expr:
        case ExplicitDate(value=value):
            return value
        case Today():
            return today
        case Tomorrow():
            return today + timedelta(days=1)
        case InDays(days=days):
            return today + timedelta(days=days)
        case InWeeks(weeks=weeks):
            return today + timedelta(weeks=weeks)
        case InMonths(months=months):
            return add_months(today, months)
        case WithinWeek():
            return today + timedelta(days=7)
        case NextWeek():
            start, end = next_week_range(today)
            raise NeedsClarification(
                f"«На следующей неделе» — это {start:%d.%m}–{end:%d.%m}. Какой день поставить?",
                code="next_week_needs_day",
                options=[{"date": (start + timedelta(days=i)).isoformat()} for i in range(7)],
            )
        case EndOfNextWeek():
            return next_week_range(today)[1]
        case NearestWeekday(weekday=weekday):
            return today + timedelta(days=(weekday - today.weekday()) % 7)
        case DayOfMonth(day=day):
            candidate = today.replace(day=min(day, last_day_of_month(today.year, today.month)))
            if candidate < today:
                candidate = add_months(today.replace(day=1), 1, anchor_day=day)
            return candidate
    raise ValidationFailed("Неизвестное выражение даты", code="bad_date_expr")


def resolve_time(expr: TimeExpr, ctx: ResolveContext) -> time:
    match expr:
        case ExactTime(value=value):
            return value.replace(tzinfo=None, second=0, microsecond=0)
        case EveningSlot():
            return ctx.evening
    raise ValidationFailed("Неизвестное выражение времени", code="bad_time_expr")


def resolve_when(when: When, ctx: ResolveContext) -> tuple[date, time | None]:
    """Resolve a due/reminder expression. A time already passed today needs clarification."""
    day = resolve_date(when.date, ctx.today)
    if when.time is None:
        return day, None
    at = resolve_time(when.time, ctx)
    if day == ctx.today and at <= ctx.local_now.time():
        raise NeedsClarification(
            f"Сегодня {at:%H:%M} уже прошло. Назначить на другое время или день?",
            code="time_passed",
        )
    if day < ctx.today:
        raise NeedsClarification(
            "Эта дата уже прошла. Какую дату иметь в виду?", code="date_passed"
        )
    return day, at


def resolve_reminder_instant(expr: When | Later, ctx: ResolveContext) -> datetime:
    """Instant for an explicit reminder/snooze. Date without time -> default notice time."""
    if isinstance(expr, Later):
        minutes = expr.minutes if expr.minutes is not None else ctx.later_minutes
        return ctx.received_at + timedelta(minutes=minutes)
    day, at = resolve_when(expr, ctx)
    if at is None:
        at = ctx.notice_time
        if day == ctx.today and at <= ctx.local_now.time():
            raise NeedsClarification(
                f"Сегодня {at:%H:%M} уже прошло. Во сколько напомнить?", code="time_passed"
            )
    return local_to_utc(day, at, ctx.tz)


def resolve_event_instant(
    expr: DateExpr | None, at: TimeExpr | None, ctx: ResolveContext
) -> datetime:
    """Instant of a past fact («пополнил вчера»). Date only keeps the receive time of day."""
    if expr is None and at is None:
        return ctx.received_at
    day = resolve_date(expr, ctx.today) if expr is not None else ctx.today
    if at is not None:
        wall = resolve_time(at, ctx)
    elif day == ctx.today:
        return ctx.received_at
    else:
        wall = ctx.local_now.time()
    instant = local_to_utc(day, wall, ctx.tz)
    if instant > ctx.received_at:
        raise ValidationFailed("Событие в будущем записать нельзя", code="future_event")
    return instant
