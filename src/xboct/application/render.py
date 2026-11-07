"""Deterministic Russian text for results and lists. Facts come only from stored data."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from xboct.domain.balance import BalanceForecast
from xboct.domain.coverage import paid_through
from xboct.domain.money import format_minor

MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
MONTHS_NOM = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)  # fmt: skip
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return few
    return many


def days(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def fmt_date(d: date, today: date | None = None) -> str:
    text = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    if today is None or d.year != today.year:
        text += f" {d.year}"
    return f"{text} ({WEEKDAYS[d.weekday()]})"


def fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def fmt_instant(instant: datetime, tz: ZoneInfo, today: date | None = None) -> str:
    local = instant.astimezone(tz)
    return f"{fmt_date(local.date(), today)} в {fmt_time(local.time())}"


def fmt_period(period_key: str, nominal: date, today: date | None = None) -> str:
    if period_key.startswith("M:"):
        return f"{MONTHS_NOM[nominal.month - 1]} {nominal.year}"
    if period_key.startswith("Y:"):
        return f"{nominal.year} год"
    return fmt_date(nominal, today)


def fmt_money(minor: int | Decimal, currency: str) -> str:
    return format_minor(minor, currency)


def coverage_text(coverage_until: date, today: date | None = None) -> str:
    """coverage_until is the first unpaid day; reaching it means the coverage expired."""
    last_paid = fmt_date(paid_through(coverage_until), today)
    if today is not None and coverage_until <= today:
        return f"оплата истекла: последний оплаченный день — {last_paid}"
    return (
        f"оплачено по {last_paid} включительно; продлить не позже этого дня, "
        f"с {fmt_date(coverage_until, today)} уже не оплачено"
    )


def coverage_reminder_text(kind: str, coverage_until: date, today: date | None = None) -> str:
    """Fact line for coverage notifications (sent by the stage 2 sender)."""
    last_paid = fmt_date(paid_through(coverage_until), today)
    if kind == "advance":
        return f"Скоро кончается оплата: последний оплаченный день — {last_paid}."
    if kind == "due":
        return (
            "Сегодня последний оплаченный день. "
            f"С {fmt_date(coverage_until, today)} уже не оплачено."
        )
    if kind == "overdue":
        return f"Оплата истекла (последний оплаченный день — {last_paid}), продления не отмечено."
    raise ValueError(kind)


def forecast_brief(fc: BalanceForecast) -> str:
    """One short phrase for the home screen: approximate days and the action, no money, no instant.

    Always an estimate by wording of the caller; never says that anything is switched off.
    """
    if fc.status == "no_baseline":
        return "нет стартового остатка — укажи остаток или «считать от нуля»"
    if fc.status == "no_rate":
        return "расход в день не задан — запас не оценить"
    if fc.status == "stopped":
        return "списание остановлено (расход 0)"
    if fc.status == "depleted":
        return "расчётный запас исчерпан — пора пополнить"
    left = fc.days_left_floor or 0
    amount = "меньше суток" if left < 1 else f"примерно {days(left)}"
    return f"{amount}, пора пополнить" if fc.status == "low" else amount


def forecast_text(
    fc: BalanceForecast, currency: str, tz: ZoneInfo, today: date | None = None
) -> str:
    """Always marked as an estimate; never claims a verified balance or a shutdown."""
    basis = ""
    if fc.baseline_at is not None:
        basis_kind = "допущение «до пополнения было 0»" if fc.baseline_assumed else "сверка"
        basis = f" Основа: {basis_kind} от {fmt_date(fc.baseline_at.astimezone(tz).date(), today)}"
        if fc.rate_minor_per_day is not None:
            basis += f", расход {fmt_money(fc.rate_minor_per_day, currency)}/день"
        basis += "."
    if fc.status == "no_baseline":
        return (
            "Прогноза нет: неизвестен стартовый остаток. Укажи примерный остаток "
            "или явно скажи «считать от нуля»."
        )
    assert fc.estimate_minor is not None
    estimate = fmt_money(fc.estimate_minor.to_integral_value(), currency)
    if fc.status == "no_rate":
        return f"По расчёту осталось примерно {estimate}; расход в день не задан.{basis}"
    if fc.status == "stopped":
        return (
            f"По расчёту осталось примерно {estimate}; расход 0 — списание остановлено, "
            f"срока исчерпания нет.{basis}"
        )
    assert fc.depletion_at is not None
    if fc.status == "depleted":
        return (
            f"Расчётный запас исчерпан (примерно {fmt_instant(fc.depletion_at, tz, today)}). "
            "Это оценка по расходу, а не данные провайдера."
            f"{basis}"
        )
    left = fc.days_left_floor or 0
    text = (
        f"По расчёту осталось примерно {estimate} — хватит примерно на {days(left)} "
        f"(до {fmt_instant(fc.depletion_at, tz, today)})."
    )
    if fc.status == "low":
        text += " Пора пополнить."
    return text + basis + " Это оценка, не проверенный баланс."
