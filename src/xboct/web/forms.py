"""HTML form fields -> typed application commands.

Parsing only: what the owner typed becomes absolute dates, minor units and ids. Every
rule (versions, kinds, states) is still checked by the application layer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from xboct.application import commands as c
from xboct.domain.errors import ValidationFailed
from xboct.domain.money import parse_major
from xboct.domain.timeutil import local_to_utc
from xboct.storage.models import Obligation, Recurrence, Task

LABELS = {
    "title": "Название",
    "note": "Заметка",
    "url": "Ссылка",
    "next_action": "Следующий шаг",
    "due_date": "Дата",
    "due_time": "Время",
    "recurrence": "Повтор",
    "interval": "Интервал",
    "monthday": "День месяца",
    "aliases": "Другие названия",
    "currency": "Валюта",
    "amount_minor": "Сумма",
    "rate_minor_per_day": "Расход в день",
    "count": "Длительность",
    "timezone": "Часовой пояс",
}
IDEM = re.compile(r"^[0-9a-f]{32}$")


class FormError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def explain(exc: ValidationError) -> list[str]:
    """Pydantic errors in plain Russian (no internals, no echo of the input)."""
    out = []
    for err in exc.errors():
        field = next((str(p) for p in reversed(err["loc"]) if str(p) in LABELS), None)
        label = LABELS.get(field or "", "Поле")
        message = str(err.get("msg", ""))
        if message.startswith("Value error, "):
            out.append(f"{label}: {message.removeprefix('Value error, ')}")
        elif err["type"] in ("string_too_long", "too_long"):
            out.append(f"{label}: слишком длинно")
        elif err["type"] in ("string_too_short", "missing"):
            out.append(f"{label}: обязательно")
        else:
            out.append(f"{label}: недопустимое значение")
    return out


def build(model: type[c._Command], **fields: Any) -> Any:
    try:
        return model(**fields)
    except ValidationError as exc:
        raise FormError(explain(exc)) from None


class Form:
    """Reads one submitted form, collecting every error before failing."""

    def __init__(self, data: Mapping[str, Any], tz: ZoneInfo, today: date | None = None) -> None:
        self.data = data
        self.tz = tz
        self.today = today
        self.errors: list[str] = []

    def text(self, name: str) -> str | None:
        value = self.data.get(name)
        if not isinstance(value, str):
            return None
        value = value.replace("\r\n", "\n").strip()
        return value or None

    def all(self, name: str) -> list[str]:
        getlist = getattr(self.data, "getlist", None)
        values = getlist(name) if getlist else [self.data.get(name)]
        return [v for v in values if isinstance(v, str) and v.strip()]

    def flag(self, name: str) -> bool:
        return self.text(name) in ("1", "on", "true", "yes")

    def required(self, name: str) -> str:
        value = self.text(name)
        if value is None:
            self.errors.append(f"{LABELS.get(name, name)}: обязательно")
            return ""
        return value

    def date(self, name: str, label: str) -> date | None:
        value = self.text(name)
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            self.errors.append(f"{label}: нужна дата ГГГГ-ММ-ДД")
            return None

    def time(self, name: str, label: str) -> time | None:
        value = self.text(name)
        if value is None:
            return None
        try:
            return time.fromisoformat(value).replace(second=0, microsecond=0)
        except ValueError:
            self.errors.append(f"{label}: нужно время ЧЧ:ММ")
            return None

    def int(self, name: str, label: str, lo: int, hi: int) -> int | None:
        value = self.text(name)
        if value is None:
            return None
        if not value.isdigit() or not lo <= int(value) <= hi:
            self.errors.append(f"{label}: целое число {lo}..{hi}")
            return None
        return int(value)

    def money(self, name: str, label: str, currency: str) -> int | None:
        value = self.text(name)
        if value is None:
            return None
        try:
            return parse_major(value, currency)
        except ValidationFailed:
            self.errors.append(f"{label}: число вроде 1500 или 1500,50")
            return None

    def instant(self, date_name: str, time_name: str, label: str) -> datetime | None:
        """Local date (+ optional time) -> aware instant; None means "now" for the handler.

        A past date without a time means its noon; today without a time means now.
        """
        day = self.date(date_name, label)
        at = self.time(time_name, label)
        if day is None:
            if at is not None:
                self.errors.append(f"{label}: время без даты")
            return None
        if at is None and day == self.today:
            return None
        return local_to_utc(day, at or time(12, 0), self.tz)

    def check(self) -> None:
        if self.errors:
            raise FormError(self.errors)


# --- recurrence ------------------------------------------------------------------------


def recurrence(form: Form, *, default_anchor: date | None) -> c.RecurrenceSpec | None:
    frequency = form.text("frequency")
    if frequency is None:
        return None
    if frequency not in ("daily", "weekly", "monthly", "yearly"):
        form.errors.append("Повтор: неизвестная частота")
        return None
    anchor = form.date("anchor_date", "Первый срок") or default_anchor
    if anchor is None:
        form.errors.append("Первый срок: обязательно для повтора")
        return None
    weekdays = []
    for value in form.all("weekdays"):
        if value.isdigit() and 0 <= int(value) <= 6:
            weekdays.append(int(value))
    form.check()
    return build(
        c.RecurrenceSpec,
        frequency=frequency,
        anchor_date=anchor,
        interval=form.int("interval", "Интервал", 1, 366) or 1,
        weekdays=sorted(set(weekdays)) if frequency == "weekly" else [],
        monthday=form.int("monthday", "День месяца", 1, 31) if frequency == "monthly" else None,
        due_time=form.time("rule_time", "Время повтора"),
    )


def same_rule(rec: Recurrence, spec: c.RecurrenceSpec) -> bool:
    return (
        rec.frequency == spec.frequency
        and rec.interval == spec.interval
        and rec.anchor_date == spec.anchor_date
        and list(rec.weekdays or []) == list(spec.weekdays)
        and rec.monthday == spec.monthday
        and rec.due_time == spec.due_time
    )


# --- tasks -----------------------------------------------------------------------------


def create_task(data: Mapping[str, Any], tz: ZoneInfo, today: date) -> c.CreateTask:
    form = Form(data, tz)
    title = form.required("title")
    due_date = form.date("due_date", "Дата")
    due_time = form.time("due_time", "Время")
    rule = recurrence(form, default_anchor=due_date or today)
    form.check()
    return build(
        c.CreateTask,
        title=title,
        note=form.text("note"),
        url=form.text("url"),
        next_action=form.text("next_action"),
        due_date=None if rule else due_date,
        due_time=None if rule else due_time,
        recurrence=rule,
    )


def update_task(
    data: Mapping[str, Any], tz: ZoneInfo, task: Task, rec: Recurrence | None
) -> c.UpdateTask:
    """Only fields that differ from the stored task are sent (absent = unchanged)."""
    form = Form(data, tz)
    version = form.int("version", "Версия", 1, 10**9)
    changes: dict[str, Any] = {}
    title = form.required("title")
    if title and title != task.title:
        changes["title"] = title
    for name in ("note", "url", "next_action"):
        value = form.text(name)
        if value != getattr(task, name):
            changes[name] = value
    if rec is None:
        due_date = form.date("due_date", "Дата")
        due_time = form.time("due_time", "Время")
        current_date = task.due_at.astimezone(tz).date() if task.due_at else task.due_date
        current_time = task.due_at.astimezone(tz).time() if task.due_at else None
        if due_date != current_date or due_time != current_time:
            changes["due_date"] = due_date
            changes["due_time"] = due_time
    else:
        spec = recurrence(form, default_anchor=rec.anchor_date)
        if spec is not None and not same_rule(rec, spec):
            changes["recurrence"] = spec
    form.check()
    if not changes:
        raise FormError(["Ничего не изменилось"])
    return build(c.UpdateTask, task_id=task.id, expected_version=version or 1, **changes)


def snooze_until(
    choice: str | None,
    custom: datetime | None,
    *,
    now: datetime,
    later_minutes: int,
    evening: time,
    morning: time,
    tz: ZoneInfo,
) -> datetime:
    local_now = now.astimezone(tz)
    if choice == "later":
        return now + timedelta(minutes=later_minutes)
    if choice == "evening":
        target = local_to_utc(local_now.date(), evening, tz)
        return (
            target
            if target > now
            else local_to_utc(local_now.date() + timedelta(days=1), evening, tz)
        )
    if choice == "tomorrow":
        return local_to_utc(local_now.date() + timedelta(days=1), morning, tz)
    if choice == "custom" and custom is not None:
        return custom
    raise FormError(["Выбери, когда напомнить"])


# --- obligations -----------------------------------------------------------------------


def aliases(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()][:20]


def create_obligation(data: Mapping[str, Any], tz: ZoneInfo, today: date) -> c.CreateObligation:
    form = Form(data, tz)
    title = form.required("title")
    kind = form.text("kind")
    if kind not in ("calendar", "coverage", "balance"):
        form.errors.append("Тип: выбери один из трёх")
    currency = (form.text("currency") or "RUB").upper()
    if currency not in ("RUB", "USD", "EUR"):
        form.errors.append("Валюта: RUB, USD или EUR")
        currency = "RUB"
    fields: dict[str, Any] = {
        "title": title,
        "kind": kind,
        "currency": currency,
        "aliases": aliases(form.text("aliases")),
        "amount_minor": form.money("amount", "Сумма", currency),
        "note": form.text("note"),
    }
    if kind == "calendar":
        if form.text("frequency") is None:
            form.errors.append("Повтор: для календарного платежа нужна частота")
        fields["recurrence"] = recurrence(form, default_anchor=None)
    elif kind == "coverage":
        last_paid = form.date("paid_through", "Оплачено по")
        fields["coverage_until"] = last_paid + timedelta(days=1) if last_paid else None
    elif kind == "balance":
        checkpoint = form.money("balance", "Остаток сейчас", currency)
        assume_zero = form.flag("assume_zero")
        if checkpoint is not None and assume_zero:
            form.errors.append("Остаток: либо сумма, либо «считать от нуля»")
        rate = form.money("rate", "Расход в день", currency)
        if checkpoint is not None or assume_zero or rate is not None:
            fields["balance"] = c.BalanceStart(
                checkpoint_minor=checkpoint,
                assume_zero=assume_zero and checkpoint is None,
                rate_minor_per_day=rate,
            )
    form.check()
    return build(c.CreateObligation, **fields)


def update_obligation(
    data: Mapping[str, Any], tz: ZoneInfo, ob: Obligation, rec: Recurrence | None
) -> c.UpdateObligation:
    form = Form(data, tz)
    version = form.int("version", "Версия", 1, 10**9)
    changes: dict[str, Any] = {}
    title = form.required("title")
    if title and title != ob.title:
        changes["title"] = title
    new_aliases = aliases(form.text("aliases"))
    if new_aliases != list(ob.aliases or []):
        changes["aliases"] = new_aliases
    amount = form.money("amount", "Сумма", ob.currency)
    if amount != ob.amount_minor:
        changes["amount_minor"] = amount
    note = form.text("note")
    if note != ob.note:
        changes["note"] = note
    if rec is not None:
        spec = recurrence(form, default_anchor=rec.anchor_date)
        if spec is not None and not same_rule(rec, spec):
            changes["recurrence"] = spec
    form.check()
    if not changes:
        raise FormError(["Ничего не изменилось"])
    return build(c.UpdateObligation, obligation_id=ob.id, expected_version=version or 1, **changes)


def mark_payment(
    data: Mapping[str, Any], tz: ZoneInfo, ob: Obligation, today: date
) -> c.MarkPayment:
    form = Form(data, tz, today)
    refs = []
    for value in form.all("occ"):
        occ_id, _, version = value.partition(":")
        if not version.isdigit():
            form.errors.append("Период: неверная отметка")
            continue
        refs.append({"id": occ_id, "expected_version": int(version)})
    if not refs:
        form.errors.append("Отметь хотя бы один период")
    amount = form.money("amount", "Сумма", ob.currency)
    paid_at = form.instant("paid_date", "paid_time", "Дата оплаты")
    form.check()
    return build(
        c.MarkPayment, obligation_id=ob.id, occurrences=refs, amount_minor=amount, paid_at=paid_at
    )


def extend_coverage(
    data: Mapping[str, Any], tz: ZoneInfo, ob: Obligation, today: date
) -> c.ExtendCoverage:
    form = Form(data, tz, today)
    version = form.int("version", "Версия", 1, 10**9)
    mode = form.text("mode") or "length"
    fields: dict[str, Any] = {}
    if mode == "until":
        last_paid = form.date("paid_through", "Оплачено по")
        if last_paid is None:
            form.errors.append("Оплачено по: обязательно")
        else:
            fields["until"] = last_paid + timedelta(days=1)
    else:
        unit = form.text("unit")
        if unit not in ("day", "week", "month", "year"):
            form.errors.append("Единица: день, неделя, месяц или год")
        fields["unit"] = unit
        fields["count"] = form.int("count", "Длительность", 1, 120)
        if fields["count"] is None:
            form.errors.append("Длительность: обязательно")
    start = form.text("start")
    if start == "today":
        fields["start"] = today
    elif start == "date":
        fields["start"] = form.date("start_date", "Начало продления")
    fields["amount_minor"] = form.money("amount", "Сумма", ob.currency)
    fields["paid_at"] = form.instant("paid_date", "paid_time", "Дата оплаты")
    form.check()
    return build(c.ExtendCoverage, obligation_id=ob.id, expected_version=version or 1, **fields)


def balance_event(
    data: Mapping[str, Any], tz: ZoneInfo, ob: Obligation, kind: str, today: date
) -> Any:
    form = Form(data, tz, today)
    label = {"topup": "Пополнение", "correct": "Остаток", "rate": "Расход в день"}[kind]
    amount = form.money("amount", label, ob.currency)
    if amount is None and not form.errors:
        form.errors.append(f"{label}: обязательно")
    effective_at = form.instant("at_date", "at_time", "Когда")
    form.check()
    assert amount is not None
    if kind == "topup":
        return build(
            c.RecordTopup,
            obligation_id=ob.id,
            amount_minor=amount,
            effective_at=effective_at,
            start_from_zero=form.flag("start_from_zero"),
        )
    if kind == "correct":
        return build(
            c.CorrectBalance, obligation_id=ob.id, amount_minor=amount, effective_at=effective_at
        )
    return build(
        c.ChangeDailyRate, obligation_id=ob.id, rate_minor_per_day=amount, effective_at=effective_at
    )


# --- settings --------------------------------------------------------------------------

SETTING_TIMES = ("quiet_start", "quiet_end", "digest_time", "default_notice_time", "evening_time")
SETTING_INTS = {
    "digest_weekday": (0, 6),
    "later_minutes": (5, 24 * 60),
    "payment_advance_days": (0, 30),
    "balance_threshold_days": (0, 60),
    "overdue_followup_hours": (1, 168),
    "max_auto_messages_per_day": (1, 20),
}
SETTING_LABELS = {
    "quiet_start": "Тихие часы с",
    "quiet_end": "Тихие часы до",
    "digest_time": "Время сводки",
    "default_notice_time": "Время напоминаний по датам",
    "evening_time": "«Вечером» — это",
    "digest_weekday": "День сводки",
    "later_minutes": "«Позже» — через минут",
    "payment_advance_days": "Напоминать о платеже заранее, дней",
    "balance_threshold_days": "Порог запаса, дней",
    "overdue_followup_hours": "Повтор после срока, часов",
    "max_auto_messages_per_day": "Автосообщений в день, максимум",
}


def update_settings(data: Mapping[str, Any], tz: ZoneInfo, current: Any) -> c.UpdateSettings:
    form = Form(data, tz)
    version = form.int("version", "Версия", 1, 10**9)
    changes: dict[str, Any] = {}
    for name in SETTING_TIMES:
        value = form.time(name, SETTING_LABELS[name])
        if value is None and not form.errors:
            form.errors.append(f"{SETTING_LABELS[name]}: обязательно")
        if value is not None and value != getattr(current, name):
            changes[name] = value
    for name, (lo, hi) in SETTING_INTS.items():
        value = form.int(name, SETTING_LABELS[name], lo, hi)
        if value is None and form.text(name) is None:
            form.errors.append(f"{SETTING_LABELS[name]}: обязательно")
        if value is not None and value != getattr(current, name):
            changes[name] = value
    profanity = form.flag("profanity_enabled")
    if profanity != current.profanity_enabled:
        changes["profanity_enabled"] = profanity
    form.check()
    if not changes:
        raise FormError(["Ничего не изменилось"])
    return build(c.UpdateSettings, expected_version=version or 1, **changes)
