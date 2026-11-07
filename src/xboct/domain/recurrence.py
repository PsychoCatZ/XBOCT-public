"""Calendar recurrence rules. Months are calendar months, never N*30 days.

Each nominal date is computed from the anchor by index, so clamping never drifts:
monthly on 31 Jan -> 28/29 Feb -> 31 Mar.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from xboct.domain.errors import ValidationFailed
from xboct.domain.timeutil import add_months, last_day_of_month, monday_of, months_between

Frequency = Literal["daily", "weekly", "monthly", "yearly"]
FREQUENCIES: tuple[str, ...] = ("daily", "weekly", "monthly", "yearly")


@dataclass(frozen=True)
class RecurrenceRule:
    frequency: Frequency
    anchor_date: date
    interval: int = 1
    weekdays: tuple[int, ...] = field(default=())
    monthday: int | None = None

    def __post_init__(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ValidationFailed(f"Неизвестная частота: {self.frequency}", code="bad_recurrence")
        if not 1 <= self.interval <= 366:
            raise ValidationFailed("Интервал повтора 1..366", code="bad_recurrence")
        if any(not 0 <= w <= 6 for w in self.weekdays):
            raise ValidationFailed("День недели 0..6", code="bad_recurrence")
        if self.weekdays and self.frequency != "weekly":
            raise ValidationFailed("Дни недели только для weekly", code="bad_recurrence")
        if self.monthday is not None:
            if self.frequency != "monthly":
                raise ValidationFailed("День месяца только для monthly", code="bad_recurrence")
            if not 1 <= self.monthday <= 31:
                raise ValidationFailed("День месяца 1..31", code="bad_recurrence")

    @property
    def day_of_month(self) -> int:
        return self.monthday if self.monthday is not None else self.anchor_date.day

    def period_key(self, nominal: date) -> str:
        if self.frequency == "monthly":
            return f"M:{nominal:%Y-%m}"
        if self.frequency == "yearly":
            return f"Y:{nominal:%Y}"
        return f"D:{nominal.isoformat()}"

    def first_date(self) -> date:
        return next(self.dates_between(self.anchor_date, date.max))

    def dates_between(self, start: date, end: date) -> Iterator[date]:
        """Nominal dates in [start, end], never before the anchor."""
        start = max(start, self.anchor_date)
        if start > end:
            return
        if self.frequency == "daily":
            yield from self._daily(start, end)
        elif self.frequency == "weekly":
            yield from self._weekly(start, end)
        elif self.frequency == "monthly":
            yield from self._monthly(start, end)
        else:
            yield from self._yearly(start, end)

    def _daily(self, start: date, end: date) -> Iterator[date]:
        offset = (start - self.anchor_date).days
        k = -(-offset // self.interval)
        current = self.anchor_date + timedelta(days=k * self.interval)
        while current <= end:
            yield current
            current += timedelta(days=self.interval)

    def _weekly(self, start: date, end: date) -> Iterator[date]:
        weekdays = sorted(set(self.weekdays)) or [self.anchor_date.weekday()]
        anchor_week = monday_of(self.anchor_date)
        weeks = (monday_of(start) - anchor_week).days // 7
        k = max(0, weeks // self.interval)
        while True:
            week = anchor_week + timedelta(weeks=k * self.interval)
            if week > end:
                return
            for weekday in weekdays:
                current = week + timedelta(days=weekday)
                if start <= current <= end:
                    yield current
            k += 1

    def _monthly(self, start: date, end: date) -> Iterator[date]:
        k = max(0, months_between(self.anchor_date, start) // self.interval - 1)
        while True:
            current = add_months(self.anchor_date, k * self.interval, anchor_day=self.day_of_month)
            if current > end:
                return
            if current >= start:
                yield current
            k += 1

    def _yearly(self, start: date, end: date) -> Iterator[date]:
        k = max(0, (start.year - self.anchor_date.year) // self.interval - 1)
        while True:
            year = self.anchor_date.year + k * self.interval
            month = self.anchor_date.month
            current = date(year, month, min(self.anchor_date.day, last_day_of_month(year, month)))
            if current > end:
                return
            if current >= start:
                yield current
            k += 1
