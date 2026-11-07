"""Local dates, DST-safe local->UTC conversion, quiet hours and month arithmetic."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xboct.domain.errors import ValidationFailed


@lru_cache(maxsize=32)
def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed(f"Неизвестный часовой пояс: {name}", code="bad_timezone") from exc


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def to_local(instant: datetime, tz: ZoneInfo) -> datetime:
    return require_aware(instant).astimezone(tz)


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return to_local(instant, tz).date()


def local_to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Convert a local wall-clock time to a UTC instant.

    DST rules (docs/TIME_AND_PAYMENTS.md): a nonexistent local time maps to the first
    valid instant after the gap; a repeated local time maps to its first occurrence.
    """
    naive = datetime.combine(day, at.replace(tzinfo=None))
    first = naive.replace(tzinfo=tz, fold=0)
    candidate = first.astimezone(UTC)
    if candidate.astimezone(tz).replace(tzinfo=None) == naive:
        return candidate
    # Inside a gap: fold=1 uses the post-transition offset (earlier UTC),
    # fold=0 the pre-transition one (later UTC). The transition lies between.
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    hi = candidate
    if lo > hi:
        lo, hi = hi, lo
    target_offset = hi.astimezone(tz).utcoffset()
    while (hi - lo) > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == target_offset:
            hi = mid
        else:
            lo = mid
    return hi.replace(microsecond=0)


def start_of_local_day(day: date, tz: ZoneInfo) -> datetime:
    return local_to_utc(day, time(0, 0), tz)


def in_quiet_hours(local_time: time, quiet_start: time, quiet_end: time) -> bool:
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local_time < quiet_end
    return local_time >= quiet_start or local_time < quiet_end


def shift_out_of_quiet(
    instant: datetime, tz: ZoneInfo, quiet_start: time, quiet_end: time
) -> datetime:
    """Move an automatic delivery instant to the end of quiet hours. Due is unaffected."""
    local = to_local(instant, tz)
    if not in_quiet_hours(local.time(), quiet_start, quiet_end):
        return require_aware(instant)
    end_day = local.date()
    if quiet_start > quiet_end and local.time() >= quiet_start:
        end_day += timedelta(days=1)
    return local_to_utc(end_day, quiet_end, tz)


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(start: date, months: int, anchor_day: int | None = None) -> date:
    """Calendar month arithmetic with clamping to the month end.

    anchor_day keeps the original day-of-month so 31 Jan -> 28 Feb -> 31 Mar.
    """
    day = anchor_day if anchor_day is not None else start.day
    index = start.year * 12 + (start.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return date(year, month, min(day, last_day_of_month(year, month)))


def months_between(a: date, b: date) -> int:
    """Whole calendar months from a's month to b's month (day ignored)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())
