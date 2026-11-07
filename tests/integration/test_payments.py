from datetime import date, timedelta

from tests.conftest import at, local
from xboct.application import commands as c
from xboct.application.queries import agenda
from xboct.application.render import coverage_reminder_text, coverage_text
from xboct.storage.models import NotificationJob, Obligation, Occurrence, Payment


async def monthly(x, title="VPS", anchor=date(2026, 9, 15), **kw) -> Obligation:
    result = await x.ok(
        c.CreateObligation(
            title=title,
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=anchor),
            **kw,
        )
    )
    return await x.get(Obligation, result.data["obligation_id"])


async def occurrences(x, ob: Obligation) -> dict[str, Occurrence]:
    rows = await x.all(Occurrence, Occurrence.owner_id == ob.id)
    return {o.period_key: o for o in rows}


async def test_paying_october_keeps_november_and_unpaid_september(x):
    # A06: created in September, September never marked, October paid early on 8 Oct.
    x.clock.set(at(2026, 9, 1))
    ob = await monthly(x)
    x.clock.set(at(2026, 10, 8))
    occ = await occurrences(x, ob)
    october = occ["M:2026-10"]
    result = await x.ok(
        c.MarkPayment(
            obligation_id=ob.id,
            occurrences=[c.OccurrenceRef(id=october.id, expected_version=1)],
            amount_minor=50_000,
        )
    )
    assert "октябрь 2026" in result.message
    assert "Следующий срок: 15 ноября" in result.message
    assert "Срок прошёл, отметки нет: сентябрь 2026" in result.message

    occ = await occurrences(x, ob)
    assert occ["M:2026-09"].status == "open"  # not proven unpaid, not silently closed
    assert occ["M:2026-10"].status == "paid"
    assert occ["M:2026-11"].status == "open"
    assert occ["M:2026-11"].nominal_due == date(2026, 11, 15)
    payments = await x.all(Payment, Payment.obligation_id == ob.id)
    assert [(p.occurrence_id, p.amount_minor) for p in payments] == [(october.id, 50_000)]

    jobs = await x.pending_jobs(ob.id)
    assert not [j for j in jobs if j.occurrence_id == october.id]
    november = [
        (j.kind, local(j.scheduled_at)) for j in jobs if j.occurrence_id == occ["M:2026-11"].id
    ]
    assert november == [
        ("advance", at(2026, 11, 12, 10)),
        ("due", at(2026, 11, 15, 10)),
        ("overdue", at(2026, 11, 17, 9)),
    ]


async def test_new_period_does_not_close_old_debt(x):
    x.clock.set(at(2026, 9, 1))
    ob = await monthly(x)
    x.clock.set(at(2026, 11, 20))
    occ = await occurrences(x, ob)
    assert [occ[k].status for k in ("M:2026-09", "M:2026-10", "M:2026-11")] == ["open"] * 3


async def test_cannot_pay_same_period_twice(x):
    ob = await monthly(x, anchor=date(2026, 10, 15))
    october = (await occurrences(x, ob))["M:2026-10"]
    ref = c.OccurrenceRef(id=october.id, expected_version=1)
    await x.ok(c.MarkPayment(obligation_id=ob.id, occurrences=[ref]))
    again = await x.run(c.MarkPayment(obligation_id=ob.id, occurrences=[ref]))
    assert again.status == "rejected"
    assert again.code == "version_conflict"
    fresh = c.OccurrenceRef(id=october.id, expected_version=2)
    again = await x.run(c.MarkPayment(obligation_id=ob.id, occurrences=[fresh]))
    assert again.code == "occurrence_closed"
    assert len(await x.all(Payment, Payment.obligation_id == ob.id)) == 1


async def test_month_end_anchor_materialized(x):
    # A07 through storage: 31 Jan -> 28 Feb -> 31 Mar.
    x.clock.set(at(2027, 1, 10))
    ob = await monthly(x, title="Домен", anchor=date(2027, 1, 31))
    occ = await occurrences(x, ob)
    assert occ["M:2027-01"].nominal_due == date(2027, 1, 31)
    assert occ["M:2027-02"].nominal_due == date(2027, 2, 28)
    assert occ["M:2027-03"].nominal_due == date(2027, 3, 31)


async def test_skip_period_and_rule_change_affects_only_future_open(x):
    x.clock.set(at(2026, 9, 1))
    ob = await monthly(x)
    x.clock.set(at(2026, 10, 8))
    occ = await occurrences(x, ob)
    await x.ok(
        c.MarkPayment(
            obligation_id=ob.id,
            occurrences=[c.OccurrenceRef(id=occ["M:2026-11"].id, expected_version=1)],
        )
    )
    await x.ok(c.SkipOccurrence(occurrence_id=occ["M:2026-12"].id, expected_version=1))
    ob = await x.get(Obligation, ob.id)
    result = await x.ok(
        c.UpdateObligation(
            obligation_id=ob.id,
            expected_version=ob.version,
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 20)),
        )
    )
    assert "будущих открытых" in result.message
    occ = await occurrences(x, ob)
    assert occ["M:2026-09"].nominal_due == date(2026, 9, 15)  # overdue history kept
    assert occ["M:2026-09"].status == "open"
    assert occ["M:2026-10"].nominal_due == date(2026, 10, 20)
    assert occ["M:2026-11"].status == "paid"  # paid history untouched
    assert occ["M:2026-11"].nominal_due == date(2026, 11, 15)
    assert occ["M:2026-12"].status == "skipped"
    assert occ["M:2027-01"].nominal_due == date(2027, 1, 20)


async def test_archive_with_outstanding_requires_choice(x):
    x.clock.set(at(2026, 9, 1))
    ob = await monthly(x)
    x.clock.set(at(2026, 10, 8))
    result = await x.run(c.ArchiveObligation(obligation_id=ob.id, expected_version=ob.version))
    assert result.status == "clarification"
    assert result.code == "outstanding_choice"
    assert {o["outstanding"] for o in result.options} == {"keep_open", "close"}
    assert (await x.get(Obligation, ob.id)).active

    await x.ok(
        c.ArchiveObligation(
            obligation_id=ob.id, expected_version=ob.version, outstanding="keep_open"
        )
    )
    occ = await occurrences(x, ob)
    assert occ["M:2026-09"].status == "open"
    assert occ["M:2026-10"].status == "cancelled"
    assert not (await x.get(Obligation, ob.id)).active
    assert await x.pending_jobs(ob.id) == []


# --- coverage ---------------------------------------------------------------------------


async def coverage(x, until=date(2026, 10, 15)) -> Obligation:
    result = await x.ok(c.CreateObligation(title="Хостинг", kind="coverage", coverage_until=until))
    return await x.get(Obligation, result.data["obligation_id"])


async def test_three_month_extension_uses_calendar_months(x):
    # A08
    ob = await coverage(x)
    result = await x.ok(
        c.ExtendCoverage(
            obligation_id=ob.id,
            expected_version=ob.version,
            unit="month",
            count=3,
            amount_minor=300_000,
        )
    )
    assert "оплачено по 14 января 2027 (чт) включительно" in result.message
    assert "с 15 января 2027 (пт) уже не оплачено" in result.message
    ob = await x.get(Obligation, ob.id)
    assert ob.coverage_until == date(2027, 1, 15)
    payment = (await x.all(Payment, Payment.obligation_id == ob.id))[0]
    assert (payment.coverage_from, payment.coverage_until) == (
        date(2026, 10, 15),
        date(2027, 1, 15),
    )
    jobs = await x.pending_jobs(ob.id)
    # coverage_until = 15 Jan is the first unpaid day: advance 3 days before it, the last
    # timely reminder on the last paid day, one follow-up 24h after the boundary.
    assert [(j.kind, local(j.scheduled_at)) for j in jobs] == [
        ("advance", at(2027, 1, 12, 10)),
        ("due", at(2027, 1, 14, 10)),
        ("overdue", at(2027, 1, 16, 9)),  # 15 Jan 00:00 + 24h -> after quiet hours
    ]
    assert {j.period_key for j in jobs} == {"until:2027-01-15"}


async def test_coverage_last_paid_day_and_expiry_at_boundary(x):
    ob = await coverage(x, until=date(2026, 10, 9))
    # On the last paid day it is still valid and listed for today.
    view = await x.app.read(agenda)
    assert [(i.title, i.due_date) for i in view.next_7_days] == []
    assert [(i.title, i.due_date) for i in view.due_today] == [("Хостинг", date(2026, 10, 8))]
    assert (
        "сегодня последний оплаченный день"
        in coverage_reminder_text("due", date(2026, 10, 9), date(2026, 10, 8)).lower()
    )
    # As soon as coverage_until begins, the coverage is expired.
    x.clock.set(at(2026, 10, 9, 0, 5))
    view = await x.app.read(agenda)
    assert [i.title for i in view.overdue] == ["Хостинг"]
    assert "оплата истекла" in coverage_text(date(2026, 10, 9), date(2026, 10, 9))
    result = await x.run(
        c.ExtendCoverage(obligation_id=ob.id, expected_version=ob.version, unit="month", count=1)
    )
    assert result.code == "coverage_expired"
    assert "последний оплаченный день — 8 октября" in result.message


async def test_expired_coverage_asks_where_to_start(x):
    ob = await coverage(x, until=date(2026, 10, 1))
    result = await x.run(
        c.ExtendCoverage(obligation_id=ob.id, expected_version=ob.version, unit="month", count=1)
    )
    assert result.status == "clarification"
    assert result.code == "coverage_expired"
    assert (await x.get(Obligation, ob.id)).coverage_until == date(2026, 10, 1)
    await x.ok(
        c.ExtendCoverage(
            obligation_id=ob.id,
            expected_version=ob.version,
            unit="month",
            count=1,
            start=date(2026, 10, 8),
        )
    )
    assert (await x.get(Obligation, ob.id)).coverage_until == date(2026, 11, 8)


async def test_unknown_coverage_start_asks(x):
    result = await x.ok(c.CreateObligation(title="Хостинг", kind="coverage"))
    ob = await x.get(Obligation, result.data["obligation_id"])
    result = await x.run(
        c.ExtendCoverage(obligation_id=ob.id, expected_version=1, unit="month", count=3)
    )
    assert result.code == "coverage_start_unknown"


# --- balance ----------------------------------------------------------------------------


async def balance(x, title="Hetzner", **start) -> Obligation:
    result = await x.ok(
        c.CreateObligation(title=title, kind="balance", balance=c.BalanceStart(**start))
    )
    return await x.get(Obligation, result.data["obligation_id"])


async def test_1200_at_40_per_day_from_zero(x):
    # A10: 30 days; warning when 5 days remain, i.e. after 25 days.
    ob = await balance(x, assume_zero=True, rate_minor_per_day=4_000)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=120_000))
    assert result.data["days_left"] == 30
    assert result.data["forecast_status"] == "ok"
    assert "примерно на 30 дней" in result.message
    assert "оценка" in result.message
    jobs = await x.pending_jobs(ob.id)
    assert [(j.kind, local(j.scheduled_at)) for j in jobs] == [("balance_low", at(2026, 11, 2, 10))]
    assert jobs[0].scheduled_at - x.clock.now() == timedelta(days=25)


async def test_topup_rate_change_and_backdated_correction_replan(x):
    # A11
    ob = await balance(x, assume_zero=True, rate_minor_per_day=4_000)
    await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=120_000))
    x.clock.advance(days=10)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=40_000))
    assert result.data["estimate_minor"] == "120000"
    assert result.data["days_left"] == 30
    result = await x.ok(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=6_000))
    assert result.data["days_left"] == 20
    jobs = await x.pending_jobs(ob.id)
    # 1200 RUB at 60/day from 18 Oct -> zero on 7 Nov; warning 5 days earlier.
    assert [(j.kind, local(j.scheduled_at)) for j in jobs] == [("balance_low", at(2026, 11, 2, 10))]
    first_epoch = jobs[0].period_key

    # Backdated correction on 15 Oct (before the 18 Oct topup): replay from it.
    result = await x.ok(
        c.CorrectBalance(
            obligation_id=ob.id, amount_minor=30_000, effective_at=x.clock.now() - timedelta(days=3)
        )
    )
    # 300 - 3 days * 40 + 400 topup = 580 RUB at 60/day -> 9 days 16 hours.
    assert result.data["estimate_minor"] == "58000"
    assert result.data["days_left"] == 9
    jobs = await x.pending_jobs(ob.id)
    assert len(jobs) == 1
    # Threshold falls on 23 Oct 02:00 local -> delivered after quiet hours.
    assert local(jobs[0].scheduled_at) == at(2026, 10, 23, 9)
    assert jobs[0].period_key == first_epoch  # same funding epoch (the 18 Oct topup)
    history = await x.all(NotificationJob, NotificationJob.subject_id == ob.id)
    assert sum(j.status == "cancelled" for j in history) >= 2


async def test_forecast_below_zero_is_estimate_not_shutdown(x):
    # A12
    ob = await balance(x, checkpoint_minor=40_000, rate_minor_per_day=4_000)
    x.clock.advance(days=12)
    result = await x.ok(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=4_000))
    assert result.data["forecast_status"] == "depleted"
    assert "Расчётный запас исчерпан" in result.message
    assert "отключ" not in result.message.lower()
    assert "долг" not in result.message.lower()


async def test_topup_after_forecast_zero_needs_explicit_baseline(x):
    # A13
    ob = await balance(x, checkpoint_minor=40_000, rate_minor_per_day=4_000)
    x.clock.advance(days=20)
    result = await x.run(c.RecordTopup(obligation_id=ob.id, amount_minor=120_000))
    assert result.status == "clarification"
    assert result.code == "topup_after_depletion"
    assert len(await x.all(Obligation)) == 1
    result = await x.ok(
        c.RecordTopup(obligation_id=ob.id, amount_minor=120_000, start_from_zero=True)
    )
    assert result.data["estimate_minor"] == "120000"  # no imaginary -400 debt subtracted
    assert result.data["days_left"] == 30
    assert "допущение" in result.message


async def test_topup_without_baseline_asks(x):
    ob = await balance(x, rate_minor_per_day=4_000)
    result = await x.run(c.RecordTopup(obligation_id=ob.id, amount_minor=120_000))
    assert result.code == "baseline_unknown"


async def test_zero_and_negative_rate(x):
    # A24
    ob = await balance(x, checkpoint_minor=100_000, rate_minor_per_day=4_000)
    assert await x.pending_jobs(ob.id)
    result = await x.ok(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=0))
    assert result.data["forecast_status"] == "stopped"
    assert result.data["depletion_at"] is None
    assert await x.pending_jobs(ob.id) == []
    result = await x.run(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=-100))
    assert result.status == "rejected"
    assert result.code == "negative_rate"


async def test_future_balance_event_rejected(x):
    ob = await balance(x, checkpoint_minor=100_000, rate_minor_per_day=4_000)
    result = await x.run(
        c.CorrectBalance(
            obligation_id=ob.id, amount_minor=1, effective_at=x.clock.now() + timedelta(hours=1)
        )
    )
    assert result.code == "future_event"


async def test_topup_is_not_a_calendar_payment(x):
    ob = await balance(x, checkpoint_minor=0, rate_minor_per_day=100)
    result = await x.run(
        c.MarkPayment(
            obligation_id=ob.id, occurrences=[c.OccurrenceRef(id="nope", expected_version=1)]
        )
    )
    assert result.code == "wrong_kind"
