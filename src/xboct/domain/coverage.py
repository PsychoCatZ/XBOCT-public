"""Coverage periods. coverage_until is the first UNPAID local date (exclusive)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from xboct.domain.errors import ValidationFailed
from xboct.domain.timeutil import add_months

CoverageUnit = Literal["day", "week", "month", "year"]


def extend_coverage(start: date, unit: CoverageUnit, count: int) -> date:
    """Exclusive end of a paid interval starting at `start` (calendar months/years)."""
    if count < 1 or count > 120:
        raise ValidationFailed("Длительность продления 1..120", code="bad_coverage")
    if unit == "day":
        return start + timedelta(days=count)
    if unit == "week":
        return start + timedelta(weeks=count)
    if unit == "month":
        return add_months(start, count)
    if unit == "year":
        return add_months(start, 12 * count)
    raise ValidationFailed(f"Неизвестная единица: {unit}", code="bad_coverage")


def paid_through(coverage_until: date) -> date:
    """Last paid day, inclusive (UI: «оплачено по ... включительно»)."""
    return coverage_until - timedelta(days=1)
