from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from xboct.domain.balance import LedgerEvent, forecast, replay

KRSK = ZoneInfo("Asia/Krasnoyarsk")
T0 = datetime(2026, 10, 8, 10, 0, tzinfo=KRSK)


def ev(kind, at=T0, seq=1, amount=None, rate=None, assumed=False, id_=None):
    return LedgerEvent(
        id=id_ or f"{kind}{seq}",
        at=at,
        kind=kind,
        seq=seq,
        amount_minor=amount,
        rate_minor_per_day=rate,
        assumed=assumed,
    )


BASE = [
    ev("correction", seq=1, amount=0, assumed=True),
    ev("topup", seq=2, amount=120_000),
    ev("rate_change", seq=3, rate=4_000),
]


def test_reference_example_30_days_and_threshold_after_25():
    # A10 / TIME_AND_PAYMENTS: 1200 RUB at 40/day from zero.
    fc = forecast(BASE, T0, threshold_days=5)
    assert fc.status == "ok"
    assert fc.days_left == Decimal(30)
    assert fc.depletion_at == datetime(2026, 11, 7, 10, 0, tzinfo=KRSK)
    assert fc.threshold_at == datetime(2026, 11, 2, 10, 0, tzinfo=KRSK)
    assert fc.threshold_at - T0 == timedelta(days=25)


def test_ten_days_later_topup_and_rate_change():
    later = T0 + timedelta(days=10)
    assert replay(BASE, later).estimate == Decimal(80_000)
    events = [*BASE, ev("topup", at=later, seq=4, amount=40_000)]
    fc = forecast(events, later, 5)
    assert fc.estimate_minor == Decimal(120_000)
    assert fc.days_left == Decimal(30)
    events.append(ev("rate_change", at=later, seq=5, rate=6_000))
    assert forecast(events, later, 5).days_left == Decimal(20)


def test_backdated_correction_replays_from_it():
    later = T0 + timedelta(days=10)
    events = [*BASE, ev("correction", at=T0 + timedelta(days=5), seq=4, amount=50_000)]
    fc = forecast(events, later, 5)
    assert fc.estimate_minor == Decimal(30_000)  # 500 - 5 days * 40
    assert fc.status == "ok"


def test_negative_estimate_is_depleted_not_debt():
    # A12
    fc = forecast(BASE, T0 + timedelta(days=31), 5)
    assert fc.status == "depleted"
    assert fc.estimate_minor < 0
    assert fc.days_left is None
    assert fc.depletion_at == datetime(2026, 11, 7, 10, 0, tzinfo=KRSK)


def test_zero_rate_means_no_depletion():
    # A24
    events = [ev("correction", amount=50_000), ev("rate_change", seq=2, rate=0)]
    fc = forecast(events, T0 + timedelta(days=100), 5)
    assert fc.status == "stopped"
    assert fc.depletion_at is None
    assert fc.estimate_minor == Decimal(50_000)


def test_topup_without_baseline_gives_no_invented_balance():
    events = [ev("topup", amount=120_000), ev("rate_change", seq=2, rate=4_000)]
    fc = forecast(events, T0, 5)
    assert fc.status == "no_baseline"
    assert fc.estimate_minor is None


def test_low_status_inside_threshold():
    fc = forecast(BASE, T0 + timedelta(days=26), 5)
    assert fc.status == "low"
    assert fc.days_left_floor == 4
