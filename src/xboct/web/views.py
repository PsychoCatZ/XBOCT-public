"""Read models for web pages. Everything shown comes from stored data; nothing mutates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from xboct.application.balance_ledger import load_forecast
from xboct.application.queries import Agenda, AgendaItem, agenda, payments
from xboct.application.render import (
    coverage_text,
    fmt_date,
    fmt_instant,
    fmt_money,
    fmt_period,
    forecast_brief,
    forecast_text,
)
from xboct.application.uow import Uow
from xboct.domain.balance import BalanceForecast
from xboct.domain.coverage import paid_through
from xboct.storage.models import (
    AuditEvent,
    BalanceEvent,
    CommandReceipt,
    NotificationJob,
    Obligation,
    Occurrence,
    OutboxMessage,
    Payment,
    Recurrence,
    Task,
)

FREQ = {
    "daily": "каждый день",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
    "yearly": "каждый год",
}
KIND = {
    "calendar": "Платёж по календарю",
    "coverage": "Оплаченный период",
    "balance": "Баланс с расходом",
}
JOB_KIND = {
    "advance": "заранее",
    "due": "в срок",
    "overdue": "после срока",
    "balance_low": "запас кончается",
    "snooze": "отложенное",
    "digest": "сводка",
}


@dataclass
class Row:
    """One line of a list with the action buttons it allows."""

    item: AgendaItem
    when: str
    version: int | None = None
    occurrence_version: int | None = None


async def today_view(uow: Uow) -> tuple[Agenda, dict[str, list[Row]]]:
    view = await agenda(uow)
    sections: dict[str, list[Row]] = {}
    for name, items in (
        ("overdue", view.overdue),
        ("today", view.due_today),
        ("week", view.next_7_days),
        ("undated", view.undated),
    ):
        rows = []
        for item in items:
            when = (
                fmt_instant(item.due_at, uow.tz, uow.today_now)
                if item.due_at is not None
                else fmt_date(item.due_date, uow.today_now)
            )
            row = Row(item, when if name != "undated" else "без срока")
            if item.kind == "task":
                task = await uow.session.get(Task, item.subject_id)
                row.version = task.version if task else None
            elif item.occurrence_id:
                occ = await uow.session.get(Occurrence, item.occurrence_id)
                row.occurrence_version = occ.version if occ else None
                owner = await uow.session.get(
                    Task if item.kind == "task_occurrence" else Obligation, item.subject_id
                )
                row.version = owner.version if owner else None
            rows.append(row)
        sections[name] = rows
    return view, sections


@dataclass
class HomeLine:
    """One record on the home screen, shown in exactly one block."""

    key: str
    kind: str  # task | task_occurrence | payment_occurrence | coverage | balance
    title: str
    href: str
    meta: str = ""
    next_action: str | None = None
    row: Row | None = None
    action: tuple[str, str] | None = None
    estimate: bool = False
    details: str | None = None


@dataclass
class Home:
    attention: list[HomeLine] = field(default_factory=list)
    today: list[HomeLine] = field(default_factory=list)
    week: list[HomeLine] = field(default_factory=list)
    overview: list[HomeLine] = field(default_factory=list)
    undated: int = 0
    missing: list[str] = field(default_factory=list)  # records whose data is not enough

    @property
    def calm(self) -> bool:
        return not (self.attention or self.today)


def _agenda_line(row: Row, section: str) -> HomeLine:
    it = row.item
    is_payment = it.kind == "payment_occurrence"
    parts = []
    if it.period and it.period != row.when:
        parts.append(f"за {it.period}" if is_payment else it.period)
    if section == "overdue":
        parts.append(
            "срок прошёл, отметки оплаты нет"
            if is_payment
            else "срок прошёл, выполнение не отмечено"
        )
    parts.append(row.when)
    owner = "obligations" if is_payment else "tasks"
    return HomeLine(
        key=f"{it.kind}:{it.occurrence_id or it.subject_id}",
        kind=it.kind,
        title=it.title,
        href=f"/{owner}/{it.subject_id}",
        meta=" · ".join(parts),
        next_action=it.next_action,
        row=row,
        action=("Оплатить…", f"/obligations/{it.subject_id}") if is_payment else None,
    )


def _coverage_attention_from(coverage_until: date, advance_days: int) -> date:
    """The first day the reminder rule would have spoken (planning.coverage_jobs).

    coverage_until is the first unpaid day. With advance_days > 1 the advance notice goes out
    that many days before it; otherwise the first notice is on the last paid day.
    """
    if advance_days > 1:
        return coverage_until - timedelta(days=advance_days)
    return paid_through(coverage_until)


async def home_view(uow: Uow) -> Home:
    """Home screen: what needs attention, today, the next 7 days and a compact overview.

    Dates and overdue-ness come from agenda() unchanged. Subscriptions and VPS are taken from
    payments() and shown once: in attention when a stored setting says so, else in the overview.
    """
    today = uow.today_now
    _, sections = await today_view(uow)
    home = Home(undated=len(sections["undated"]))
    for section, target in (
        ("overdue", home.attention),
        ("today", home.today),
        ("week", home.week),
    ):
        for row in sections[section]:
            if row.item.kind != "coverage":  # subscriptions are placed below, once
                target.append(_agenda_line(row, section))

    settings = uow.settings
    urgent: list[tuple[int, HomeLine]] = []
    overview: list[tuple[tuple[int, int, str], HomeLine]] = []
    for v in await payments(uow):
        href = f"/obligations/{v.obligation_id}"
        if v.kind == "coverage":
            if v.coverage_until is None:
                home.missing.append(v.title)
                overview.append(
                    (
                        (2, 0, v.title),
                        HomeLine(
                            f"coverage:{v.obligation_id}",
                            "coverage",
                            v.title,
                            href,
                            "граница оплаты неизвестна — укажи дату",
                            action=("Указать…", href),
                        ),
                    )
                )
                continue
            last_paid = paid_through(v.coverage_until)
            expired = last_paid < today
            line = HomeLine(
                f"coverage:{v.obligation_id}",
                "coverage",
                v.title,
                href,
                (
                    f"оплата истекла: последний оплаченный день — {fmt_date(last_paid, today)}"
                    if expired
                    else f"оплачено по {fmt_date(last_paid, today)} включительно"
                ),
                action=("Продлить…", href),
            )
            start = _coverage_attention_from(v.coverage_until, settings.payment_advance_days)
            if today >= start:
                if not expired:
                    line.meta += "; пора продлить"
                urgent.append((1 if expired else 3, line))
            else:
                overview.append(((1, v.coverage_until.toordinal(), v.title), line))
        elif v.kind == "balance" and v.forecast is not None:
            fc = v.forecast
            line = HomeLine(
                f"balance:{v.obligation_id}",
                "balance",
                v.title,
                href,
                forecast_brief(fc),
                action=("Пополнить…" if fc.status in ("low", "depleted") else "Открыть…", href),
                estimate=fc.status in ("ok", "low", "depleted"),
                details=forecast_text(fc, v.currency, uow.tz, today)
                if fc.status != "no_baseline"
                else None,
            )
            if fc.status in ("low", "depleted"):
                urgent.append((0 if fc.status == "depleted" else 2, line))
            else:
                if fc.status in ("no_baseline", "no_rate"):
                    home.missing.append(v.title)
                    line.action = ("Указать…", href)
                days_left = fc.days_left_floor if fc.days_left_floor is not None else 10**6
                overview.append(((0, days_left, v.title), line))
    home.attention.extend(line for _, line in sorted(urgent, key=lambda t: (t[0], t[1].title)))
    home.overview = [line for _, line in sorted(overview, key=lambda t: t[0])]
    return home


@dataclass
class PaymentRow:
    id: str
    title: str
    kind: str
    kind_label: str
    summary: str
    attention: bool
    current_occurrence: Occurrence | None = None
    last_payment: str | None = None


async def payments_view(uow: Uow) -> list[PaymentRow]:
    today = uow.today_now
    rows: list[PaymentRow] = []
    for v in await payments(uow):
        current = None
        attention = False
        if v.kind == "calendar":
            open_occ = await uow.open_occurrences("obligation", v.obligation_id)
            current = open_occ[0] if open_occ else None
            summary = (
                f"следующий срок {fmt_date(v.next_due, today)} ({v.next_period})"
                if v.next_due
                else "открытых периодов нет"
            )
            if v.outstanding:
                summary += f"; срок прошёл, отметки нет: {', '.join(v.outstanding)}"
                attention = True
        elif v.kind == "coverage":
            summary = (
                coverage_text(v.coverage_until, today)
                if v.coverage_until
                else "граница оплаты неизвестна"
            )
            attention = bool(v.coverage_until and v.coverage_until <= today + timedelta(days=3))
        else:
            assert v.forecast is not None
            summary = forecast_text(v.forecast, v.currency, uow.tz, today)
            attention = v.forecast.status in ("low", "depleted", "no_baseline")
        last = None
        if v.last_payment_at is not None:
            last = fmt_date(v.last_payment_at.astimezone(uow.tz).date(), today)
            if v.last_payment_minor is not None:
                last += f", {fmt_money(v.last_payment_minor, v.currency)}"
        rows.append(
            PaymentRow(
                v.obligation_id, v.title, v.kind, KIND[v.kind], summary, attention, current, last
            )
        )
    return rows


@dataclass
class HistoryLine:
    at: str
    text: str
    command_id: str | None = None
    undone: bool = False


async def subject_history(uow: Uow, subject_id: str, limit: int = 30) -> list[HistoryLine]:
    """What was done (receipts touching the subject) and what was sent (deliveries)."""
    command_ids = list(
        await uow.session.scalars(
            select(AuditEvent.command_id)
            .where(or_(AuditEvent.entity_id == subject_id, AuditEvent.parent_id == subject_id))
            .order_by(AuditEvent.id.desc())
        )
    )
    seen: list[str] = []
    for cid in command_ids:
        if cid not in seen:
            seen.append(cid)
    lines: list[tuple[datetime, HistoryLine]] = []
    for cid in seen[:limit]:
        receipt = await uow.session.get(CommandReceipt, cid)
        if receipt is None:
            continue
        lines.append(
            (
                receipt.applied_at,
                HistoryLine(
                    fmt_instant(receipt.applied_at, uow.tz, uow.today_now),
                    (receipt.result or {}).get("message", receipt.command_type),
                    cid,
                    receipt.undone_by is not None,
                ),
            )
        )
    messages = await uow.session.scalars(
        select(OutboxMessage).where(OutboxMessage.status == "sent", OutboxMessage.kind != "reply")
    )
    for message in messages:
        if any(s[1] == subject_id for s in message.subjects) and message.sent_at:
            lines.append(
                (
                    message.sent_at,
                    HistoryLine(
                        fmt_instant(message.sent_at, uow.tz, uow.today_now),
                        "Напоминание отправлялось в Telegram",
                    ),
                )
            )
    lines.sort(key=lambda pair: pair[0], reverse=True)
    return [line for _, line in lines[:limit]]


async def planned_jobs(uow: Uow, subject_id: str) -> list[str]:
    rows = await uow.session.scalars(
        select(NotificationJob)
        .where(NotificationJob.subject_id == subject_id, NotificationJob.status == "pending")
        .order_by(NotificationJob.scheduled_at)
        .limit(6)
    )
    return [
        f"{fmt_instant(j.scheduled_at, uow.tz, uow.today_now)} — {JOB_KIND[j.kind]}" for j in rows
    ]


@dataclass
class TaskCard:
    task: Task
    due: str
    rule: str | None
    occurrences: list[tuple[Occurrence, str, str]] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    history: list[HistoryLine] = field(default_factory=list)


async def task_card(uow: Uow, task_id: str) -> TaskCard | None:
    task = await uow.session.get(Task, task_id)
    if task is None:
        return None
    today = uow.today_now
    due = (
        fmt_instant(task.due_at, uow.tz, today)
        if task.due_at
        else fmt_date(task.due_date, today)
        if task.due_date
        else "без срока"
    )
    rule = None
    occurrences = []
    if task.recurrence_id:
        rec = await uow.session.get(Recurrence, task.recurrence_id)
        if rec:
            rule = describe_rule(rec, today)
        for occ in (await uow.open_occurrences("task", task.id))[:6]:
            occurrences.append(
                (
                    occ,
                    fmt_period(occ.period_key, occ.nominal_due, today),
                    fmt_date(occ.effective_due, today),
                )
            )
    return TaskCard(
        task,
        due,
        rule,
        occurrences,
        await planned_jobs(uow, task.id),
        await subject_history(uow, task.id),
    )


def describe_rule(rec: Recurrence, today: date) -> str:
    text = FREQ[rec.frequency] if rec.interval == 1 else f"раз в {rec.interval} ({rec.frequency})"
    text += f", от {fmt_date(rec.anchor_date, today)}"
    if rec.due_time:
        text += f" в {rec.due_time:%H:%M}"
    return text


@dataclass
class ObligationCard:
    ob: Obligation
    kind_label: str
    rule: str | None = None
    open_occurrences: list[tuple[Occurrence, str, str, bool]] = field(default_factory=list)
    coverage: str | None = None
    forecast: BalanceForecast | None = None
    forecast_text: str | None = None
    payments: list[str] = field(default_factory=list)
    ledger: list[str] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    history: list[HistoryLine] = field(default_factory=list)


async def obligation_card(uow: Uow, obligation_id: str) -> ObligationCard | None:
    ob = await uow.session.get(Obligation, obligation_id)
    if ob is None:
        return None
    today = uow.today_now
    card = ObligationCard(ob, KIND[ob.kind])
    if ob.recurrence_id:
        rec = await uow.session.get(Recurrence, ob.recurrence_id)
        if rec:
            card.rule = describe_rule(rec, today)
        for occ in (await uow.open_occurrences("obligation", ob.id))[:8]:
            card.open_occurrences.append(
                (
                    occ,
                    fmt_period(occ.period_key, occ.nominal_due, today),
                    fmt_date(occ.effective_due, today),
                    occ.effective_due < today,
                )
            )
    if ob.kind == "coverage":
        card.coverage = (
            coverage_text(ob.coverage_until, today)
            if ob.coverage_until
            else "граница оплаты неизвестна"
        )
    if ob.kind == "balance":
        card.forecast = await load_forecast(uow, ob)
        card.forecast_text = forecast_text(card.forecast, ob.currency, uow.tz, today)
        events = await uow.session.scalars(
            select(BalanceEvent)
            .where(BalanceEvent.obligation_id == ob.id)
            .order_by(BalanceEvent.effective_at.desc(), BalanceEvent.seq.desc())
            .limit(12)
        )
        for e in events:
            label = {
                "topup": "пополнение",
                "correction": "сверка остатка",
                "rate_change": "расход в день",
            }[e.kind]
            value = fmt_money(
                e.amount_minor if e.amount_minor is not None else e.rate_minor_per_day or 0,
                ob.currency,
            )
            extra = " (допущение «было 0»)" if e.assumed else ""
            extra += " — отменено" if e.reverted_at else ""
            card.ledger.append(
                f"{fmt_instant(e.effective_at, uow.tz, today)}: {label} {value}{extra}"
            )
    pays = await uow.session.scalars(
        select(Payment)
        .where(Payment.obligation_id == ob.id)
        .order_by(Payment.paid_at.desc())
        .limit(12)
    )
    for p in pays:
        text = f"{fmt_date(p.paid_at.astimezone(uow.tz).date(), today)}: оплата"
        if p.occurrence_id:
            occ = await uow.session.get(Occurrence, p.occurrence_id)
            if occ:
                text += f" за {fmt_period(occ.period_key, occ.nominal_due, today)}"
        if p.coverage_until:
            text += f", покрытие до {fmt_date(p.coverage_until, today)} (не включая)"
        if p.amount_minor is not None:
            text += f", {fmt_money(p.amount_minor, p.currency)}"
        if p.reverted_at:
            text += " — отменено"
        card.payments.append(text)
    card.jobs = await planned_jobs(uow, ob.id)
    card.history = await subject_history(uow, ob.id)
    return card


async def recent_receipts(uow: Uow, limit: int = 40) -> list[dict[str, Any]]:
    rows = await uow.session.scalars(
        select(CommandReceipt)
        .where(CommandReceipt.source != "system")
        .order_by(CommandReceipt.applied_at.desc())
        .limit(limit)
    )
    return [
        {
            "at": fmt_instant(r.applied_at, uow.tz, uow.today_now),
            "source": {
                "web": "веб",
                "telegram_update": "Telegram",
                "telegram_callback": "кнопка",
                "cli": "CLI",
                "test": "тест",
            }.get(r.source, r.source),
            "status": r.status,
            "message": (r.result or {}).get("message", ""),
            "command_id": r.command_id,
            "undoable": r.undoable and r.undone_by is None and r.status == "applied",
            "undone": r.undone_by is not None,
        }
        for r in rows
    ]


@dataclass
class ItemsView:
    tasks: list[tuple[Task, str]]
    obligations: list[tuple[Obligation, str]]
    closed_tasks: list[tuple[Task, str]]
    archived: list[Obligation]


def _matches(query: str, *texts: str | None) -> bool:
    needle = query.casefold()
    return any(t and needle in t.casefold() for t in texts)


async def items_view(uow: Uow, query: str | None = None) -> ItemsView:
    """Every record, open first. `query` is a plain substring filter, not language parsing."""
    today = uow.today_now
    view = ItemsView([], [], [], [])
    for task in await uow.session.scalars(select(Task).order_by(Task.created_at.desc())):
        if query and not _matches(query, task.title, task.note, task.next_action):
            continue
        if task.status == "open":
            due = (
                fmt_instant(task.due_at, uow.tz, today)
                if task.due_at
                else fmt_date(task.due_date, today)
                if task.due_date
                else "повторяется"
                if task.recurrence_id
                else "без срока"
            )
            view.tasks.append((task, due))
        elif len(view.closed_tasks) < 30:
            label = "выполнено" if task.status == "completed" else "отменено"
            at = task.completed_at or task.cancelled_at or task.updated_at
            when = fmt_date(at.astimezone(uow.tz).date(), today)
            view.closed_tasks.append((task, f"{label} {when}"))
    view.tasks.sort(
        key=lambda pair: (
            pair[0].due_at.astimezone(uow.tz).date()
            if pair[0].due_at
            else pair[0].due_date or date.max,
            pair[0].title.casefold(),
        )
    )
    for ob in await uow.session.scalars(select(Obligation).order_by(Obligation.title)):
        if query and not _matches(query, ob.title, ob.note, *ob.aliases):
            continue
        if ob.active:
            view.obligations.append((ob, KIND[ob.kind]))
        else:
            view.archived.append(ob)
    return view


@dataclass
class Diagnostics:
    rows: list[tuple[str, str]]
    warnings: list[str]


async def diagnostics(
    uow: Uow,
    *,
    runtime_state: str,
    llm: dict[str, Any],
    sessions: int,
    schema: str | None,
    head: str,
    backups: bool = False,
) -> Diagnostics:
    from sqlalchemy import func

    from xboct.scheduling.state import (
        LAST_BACKUP,
        LAST_REFRESH,
        LAST_TICK,
        TELEGRAM_HEALTH,
        get_state,
    )
    from xboct.storage.models import LlmCall

    today = uow.today_now
    warnings: list[str] = []

    def instant(value: str | None) -> str:
        if not value:
            return "ещё не было"
        return fmt_instant(datetime.fromisoformat(value), uow.tz, today)

    tick = await get_state(uow.session, LAST_TICK) or {}
    backup = await get_state(uow.session, LAST_BACKUP) or {}
    refresh = await get_state(uow.session, LAST_REFRESH) or {}
    health = await get_state(uow.session, TELEGRAM_HEALTH) or {}
    error = health.get("last_error") or {}
    outbox = dict(
        (
            await uow.session.execute(
                select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status)
            )
        ).all()
    )
    next_job = await uow.session.scalar(
        select(NotificationJob.scheduled_at)
        .where(NotificationJob.status == "pending")
        .order_by(NotificationJob.scheduled_at)
        .limit(1)
    )
    calls_today = await uow.session.scalar(
        select(func.coalesce(func.sum(LlmCall.attempts), 0)).where(LlmCall.local_date == today)
    )
    last_call = await uow.session.scalar(select(LlmCall).order_by(LlmCall.id.desc()).limit(1))
    if tick.get("at"):
        age = uow.now - datetime.fromisoformat(tick["at"])
        if age > timedelta(minutes=5):
            warnings.append("Планировщик давно не делал тик: напоминания могут не уходить.")
    if runtime_state != "running":
        warnings.append("Фоновый цикл (Telegram, напоминания) в этом процессе не запущен.")
    if outbox.get("unknown"):
        warnings.append("Есть сообщения с неизвестным результатом доставки: проверь Telegram.")
    if schema != head:
        warnings.append("Схема БД не совпадает с кодом.")
    if not backups:
        backup_text = "не настроена (BACKUP_DIR)"
        warnings.append("Ежедневные резервные копии не настроены.")
    elif backup.get("at"):
        backup_text = f"{instant(backup['at'])}, {backup.get('file', '')}"
        if backup.get("failed_at"):
            backup_text += f"; последняя попытка не удалась {instant(backup['failed_at'])}"
    else:
        backup_text = "ещё не делалась"
    if backup.get("failed_at") and backups:
        warnings.append("Последняя резервная копия не удалась: см. логи.")
    if backups and backup.get("at"):
        age = uow.now - datetime.fromisoformat(backup["at"])
        if age > timedelta(hours=36):
            warnings.append("Резервной копии больше полутора суток.")
    if llm.get("provider") != "openai":
        warnings.append(
            "Модель отключена (degraded): свободный текст не разбирается; "
            "формы, кнопки и напоминания работают."
        )
    last_llm = "вызовов ещё не было"
    if last_call is not None:
        last_llm = f"{instant(last_call.at.isoformat())}: {last_call.status}"
        if last_call.error_kind:
            last_llm += f" ({last_call.error_kind})"
    rows = [
        (
            "Фоновый цикл",
            {
                "running": "работает",
                "disabled": "не запущен в этом процессе",
                "stopped": "остановлен",
            }.get(runtime_state, runtime_state),
        ),
        ("Последний тик планировщика", instant(tick.get("at"))),
        ("Последний пересчёт расписания", instant(refresh.get("at"))),
        ("Telegram: последний успех", instant(health.get("last_ok"))),
        (
            "Telegram: последняя ошибка",
            f"{instant(error.get('at'))} ({error.get('kind')})" if error else "не было",
        ),
        ("Очередь: ждут отправки", str(outbox.get("pending", 0) + outbox.get("sending", 0))),
        ("Очередь: результат неизвестен", str(outbox.get("unknown", 0))),
        ("Очередь: не доставлено", str(outbox.get("failed", 0))),
        (
            "Ближайшее напоминание",
            fmt_instant(next_job, uow.tz, today) if next_job else "не запланировано",
        ),
        (
            "Модель",
            f"{llm.get('provider', '-')}" + (f", {llm['model']}" if llm.get("model") else ""),
        ),
        ("Вызовы модели сегодня", f"{calls_today} из {llm.get('limit', '-')}"),
        ("Последний вызов модели", last_llm),
        ("Активные веб-сессии", str(sessions)),
        ("Версия схемы БД", f"{schema or '-'} (код: {head})"),
        ("Резервная копия", backup_text),
    ]
    return Diagnostics(rows, warnings)
