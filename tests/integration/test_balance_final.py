"""Balance already below the warning threshold at funding (Codex, stage 4 review).

The reply warns at once (no immediate duplicate message). With more than a day left,
exactly one final warning is planned a day before the estimated zero; with a day or less
the reply says so and nothing already past is planned. Replans, cancellation, dedup and
quiet hours apply. A forecast is always an estimate, never a verified balance.
"""

from __future__ import annotations

from tests.conftest import at, local
from xboct.application import commands as c
from xboct.application.planning import FINAL_SUFFIX
from xboct.storage.models import NotificationJob, Obligation


async def vps(x, rate_minor: int = 4_000) -> Obligation:
    result = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="balance",
            balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=rate_minor),
        )
    )
    return await x.get(Obligation, result.data["obligation_id"])


def plan(jobs: list[NotificationJob]) -> list[tuple[str, bool, object]]:
    return [(j.kind, j.period_key.endswith(FINAL_SUFFIX), local(j.scheduled_at)) for j in jobs]


async def test_four_days_at_threshold_five_then_topup_replans(x):
    # Scenario from the review: 4 days left with a 5-day threshold.
    ob = await vps(x)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=16_000))
    assert result.data["days_left"] == 4
    assert "Пора пополнить." in result.message  # the warning is in the reply itself
    assert "Это оценка, не проверенный баланс." in result.message
    assert "Последнее предупреждение — 11 октября (вс) в 10:00" in result.message
    assert "отключ" not in result.message
    jobs = await x.pending_jobs(ob.id)
    assert plan(jobs) == [("balance_low", True, at(2026, 10, 11, 10))]  # no immediate one

    # A topup before the final warning goes out recalculates the plan.
    x.clock.advance(days=1)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=40_000))
    assert result.data["days_left"] == 13  # 160 - 40 + 400 = 520 at 40/day
    jobs = await x.pending_jobs(ob.id)
    assert plan(jobs) == [("balance_low", False, at(2026, 10, 17, 10))]  # 5 days before zero
    final = await x.all(NotificationJob, NotificationJob.period_key.endswith(FINAL_SUFFIX))
    assert [(j.status, j.cancel_reason) for j in final] == [("cancelled", "replanned")]


async def test_a_day_or_less_left_is_said_in_the_reply_only(x):
    ob = await vps(x)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=3_000))  # 18 hours
    assert "До расчётного нуля не больше суток" in result.message
    assert "отдельного напоминания уже не будет" in result.message
    assert await x.pending_jobs(ob.id) == []


async def test_final_warning_follows_rate_change(x):
    ob = await vps(x)
    await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=16_000))
    result = await x.ok(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=8_000))
    assert "Последнее предупреждение — 9 октября (пт) в 10:00" in result.message
    assert plan(await x.pending_jobs(ob.id)) == [("balance_low", True, at(2026, 10, 9, 10))]
    # Faster still: zero within a day, so the final warning is dropped, not sent late.
    result = await x.ok(c.ChangeDailyRate(obligation_id=ob.id, rate_minor_per_day=20_000))
    assert "не больше суток" in result.message
    assert await x.pending_jobs(ob.id) == []


async def test_final_warning_leaves_quiet_hours(x):
    # 100 RUB/hour; 8900 RUB -> zero on 12 Oct 03:00, final on 11 Oct 03:00 -> 09:00.
    ob = await vps(x, rate_minor=240_000)
    result = await x.ok(c.RecordTopup(obligation_id=ob.id, amount_minor=890_000))
    assert plan(await x.pending_jobs(ob.id)) == [("balance_low", True, at(2026, 10, 11, 9))]
    assert "Последнее предупреждение — 11 октября (вс) в 09:00" in result.message


async def test_final_warning_is_sent_once(bot):
    result = await bot.ok(
        c.CreateObligation(
            title="VPS",
            kind="balance",
            balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=4_000),
        )
    )
    ob_id = result.data["obligation_id"]
    await bot.ok(c.RecordTopup(obligation_id=ob_id, amount_minor=16_000))
    await bot.tick()
    assert bot.sent == []  # nothing immediate: the reply already warned
    bot.clock.set(at(2026, 10, 11, 10, 0))
    await bot.tick()
    assert len(bot.sent) == 1
    assert bot.sent[0].startswith("Последнее предупреждение: «VPS»: По расчёту осталось")
    assert "Это оценка, не проверенный баланс." in bot.sent[0]
    # A later replan of the same funding epoch (rate tweak) does not plan it again.
    await bot.ok(c.ChangeDailyRate(obligation_id=ob_id, rate_minor_per_day=3_000))
    pending = await bot.rows(
        NotificationJob, NotificationJob.subject_id == ob_id, NotificationJob.status == "pending"
    )
    assert pending == []
    bot.clock.set(at(2026, 10, 11, 18, 0))
    await bot.tick()
    assert len(bot.sent) == 1
