from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from xboct.domain.clock import FixedClock
from xboct.domain.timeutil import (
    add_months,
    local_date,
    local_to_utc,
    shift_out_of_quiet,
)

BERLIN = ZoneInfo("Europe/Berlin")
KRSK = ZoneInfo("Asia/Krasnoyarsk")


def test_fixed_clock_requires_aware_and_advances():
    with pytest.raises(ValueError, match="aware"):
        FixedClock(datetime(2026, 10, 8, 10, 0))  # noqa: DTZ001
    clock = FixedClock(datetime(2026, 10, 8, 10, 0, tzinfo=KRSK))
    assert clock.now() == datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
    clock.advance(days=10)
    assert clock.now() == datetime(2026, 10, 18, 3, 0, tzinfo=UTC)


def test_local_date_uses_owner_timezone_not_utc():
    instant = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)  # 01:30 next day in Krasnoyarsk
    assert local_date(instant, KRSK) == date(2026, 10, 9)


def test_nonexistent_local_time_maps_to_first_valid_instant():
    # A31: 2026-03-29 02:30 does not exist in Berlin (02:00 -> 03:00).
    instant = local_to_utc(date(2026, 3, 29), time(2, 30), BERLIN)
    assert instant == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert instant.astimezone(BERLIN).time() == time(3, 0)


def test_repeated_local_time_maps_to_first_occurrence():
    # A31: 2026-10-25 02:30 happens twice in Berlin; take the first (CEST, +02:00).
    instant = local_to_utc(date(2026, 10, 25), time(2, 30), BERLIN)
    assert instant == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_regular_local_time_roundtrip():
    instant = local_to_utc(date(2026, 10, 15), time(10, 0), KRSK)
    assert instant == datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("local_time", "expected"),
    [
        (datetime(2026, 10, 8, 23, 30, tzinfo=KRSK), datetime(2026, 10, 9, 9, 0, tzinfo=KRSK)),
        (datetime(2026, 10, 9, 3, 0, tzinfo=KRSK), datetime(2026, 10, 9, 9, 0, tzinfo=KRSK)),
        (datetime(2026, 10, 9, 9, 0, tzinfo=KRSK), datetime(2026, 10, 9, 9, 0, tzinfo=KRSK)),
        (datetime(2026, 10, 9, 22, 59, tzinfo=KRSK), datetime(2026, 10, 9, 22, 59, tzinfo=KRSK)),
    ],
)
def test_quiet_hours_move_delivery_to_their_end(local_time, expected):
    shifted = shift_out_of_quiet(local_time, KRSK, time(23, 0), time(9, 0))
    assert shifted == expected


def test_add_months_keeps_anchor_day():
    jan31 = date(2027, 1, 31)
    assert add_months(jan31, 1) == date(2027, 2, 28)
    assert add_months(jan31, 2) == date(2027, 3, 31)
    assert add_months(date(2027, 2, 28), 1, anchor_day=31) == date(2027, 3, 31)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
