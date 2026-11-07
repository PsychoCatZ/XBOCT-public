"""Command handlers. Each runs inside the caller's transaction (see service.Application).

Handlers validate, mutate, audit and mark subjects for replanning. Success text is
built from the flushed state, never from the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select

from xboct.application import commands as c
from xboct.application.balance_ledger import add_event, estimate_before, load_forecast
from xboct.application.context import Outcome
from xboct.application.duplicates import find_duplicate
from xboct.application.materialize import (
    apply_rule_change,
    ensure_materialized,
    horizon_for,
)
from xboct.application.planning import (
    FINAL_SUFFIX,
    FINAL_WARNING_BEFORE,
    replan_subject,
    snooze,
)
from xboct.application.render import (
    coverage_text,
    fmt_date,
    fmt_instant,
    fmt_money,
    fmt_period,
    forecast_text,
)
from xboct.application.uow import (
    MODELS_BY_TYPE,
    RESTORABLE,
    Uow,
    from_json,
    restorable_view,
    snapshot,
)
from xboct.domain.balance import BalanceForecast
from xboct.domain.coverage import extend_coverage, paid_through
from xboct.domain.errors import (
    InvalidState,
    NeedsClarification,
    UndoUnavailable,
    ValidationFailed,
    VersionConflict,
)
from xboct.domain.money import check_currency
from xboct.domain.recurrence import RecurrenceRule
from xboct.domain.timeutil import get_zone, local_to_utc
from xboct.storage.models import (
    AuditEvent,
    CommandReceipt,
    NotificationJob,
    Obligation,
    Occurrence,
    Payment,
    PendingIntent,
    Recurrence,
    Task,
)

Handler = Callable[[Uow, Any], Awaitable[Outcome]]

FREQUENCY_TEXT = {
    "daily": "ежедневно",
    "weekly": "еженедельно",
    "monthly": "ежемесячно",
    "yearly": "ежегодно",
}


# --- shared helpers -------------------------------------------------------------------


async def apply_replans(uow: Uow) -> None:
    while uow.replan:
        subject_type, subject_id = sorted(uow.replan)[0]
        uow.replan.discard((subject_type, subject_id))
        await replan_subject(uow, subject_type, subject_id)


async def next_job(uow: Uow, subject_type: str, subject_id: str) -> NotificationJob | None:
    return await uow.session.scalar(
        select(NotificationJob)
        .where(
            NotificationJob.subject_type == subject_type,
            NotificationJob.subject_id == subject_id,
            NotificationJob.status == "pending",
        )
        .order_by(NotificationJob.scheduled_at)
        .limit(1)
    )


async def reminder_line(uow: Uow, subject_type: str, subject_id: str) -> str:
    job = await next_job(uow, subject_type, subject_id)
    if job is None:
        return "Напоминаний не запланировано."
    text = f"Напомню {fmt_instant(job.scheduled_at, uow.tz, uow.today)}"
    if job.kind in ("due", "advance") and not job.explicit:
        text += " (время по умолчанию, можно изменить)"
    elif job.kind == "balance_low" and job.period_key.endswith(FINAL_SUFFIX):
        text = (
            f"Последнее предупреждение — {fmt_instant(job.scheduled_at, uow.tz, uow.today)}, "
            "примерно за сутки до расчётного нуля"
        )
    elif job.kind == "balance_low":
        text = (
            f"Предупрежу {fmt_instant(job.scheduled_at, uow.tz, uow.today)} — примерно за "
            f"{uow.settings.balance_threshold_days} дн. до расчётного нуля"
        )
    return text + "."


async def balance_plan_line(uow: Uow, ob: Obligation, fc: BalanceForecast) -> str:
    """What happens next for a balance: the planned warning, or why there is none."""
    if fc.status not in ("ok", "low") or fc.depletion_at is None:
        return ""
    if await next_job(uow, "obligation", ob.id) is not None:
        return await reminder_line(uow, "obligation", ob.id)
    if fc.depletion_at - uow.now <= FINAL_WARNING_BEFORE:
        return (
            "До расчётного нуля не больше суток — отдельного напоминания уже не будет, "
            "пополни заранее."
        )
    return ""


def rule_from_spec(spec: c.RecurrenceSpec) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=spec.frequency,
        anchor_date=spec.anchor_date,
        interval=spec.interval,
        weekdays=tuple(spec.weekdays),
        monthday=spec.monthday,
    )


async def create_recurrence(
    uow: Uow, owner_type: str, owner_id: str, spec: c.RecurrenceSpec
) -> Recurrence:
    rule_from_spec(spec)  # validates
    recurrence = Recurrence(
        owner_type=owner_type,
        owner_id=owner_id,
        frequency=spec.frequency,
        interval=spec.interval,
        weekdays=list(spec.weekdays),
        monthday=spec.monthday,
        anchor_date=spec.anchor_date,
        due_time=spec.due_time,
        created_at=uow.now,
        updated_at=uow.now,
    )
    uow.session.add(recurrence)
    await uow.session.flush()
    await ensure_materialized(uow, recurrence, horizon_for(uow.today_now))
    return recurrence


def check_not_future(uow: Uow, at: datetime | None) -> datetime:
    at = at or uow.ctx.received_at
    if at > uow.ctx.received_at:
        raise ValidationFailed("Событие в будущем записать нельзя", code="future_event")
    return at


def task_due_text(uow: Uow, task: Task) -> str:
    if task.due_at is not None:
        return f"срок {fmt_instant(task.due_at, uow.tz, uow.today)}"
    if task.due_date is not None:
        return f"срок {fmt_date(task.due_date, uow.today)}"
    return "без срока"


async def next_open(uow: Uow, owner_type: str, owner_id: str) -> Occurrence | None:
    return await uow.session.scalar(
        select(Occurrence)
        .where(
            Occurrence.owner_type == owner_type,
            Occurrence.owner_id == owner_id,
            Occurrence.status == "open",
            Occurrence.effective_due >= uow.today,
        )
        .order_by(Occurrence.effective_due)
        .limit(1)
    )


async def outstanding(uow: Uow, owner_type: str, owner_id: str) -> list[Occurrence]:
    rows = await uow.session.scalars(
        select(Occurrence)
        .where(
            Occurrence.owner_type == owner_type,
            Occurrence.owner_id == owner_id,
            Occurrence.status == "open",
            Occurrence.effective_due < uow.today,
        )
        .order_by(Occurrence.effective_due)
    )
    return list(rows)


def period_text(uow: Uow, occ: Occurrence) -> str:
    return fmt_period(occ.period_key, occ.nominal_due, uow.today)


async def occurrence_summary(uow: Uow, owner_type: str, owner_id: str) -> str:
    parts: list[str] = []
    upcoming = await next_open(uow, owner_type, owner_id)
    if upcoming is not None:
        parts.append(f"Следующий срок: {fmt_date(upcoming.effective_due, uow.today)}.")
    late = await outstanding(uow, owner_type, owner_id)
    if late:
        items = ", ".join(
            f"{period_text(uow, o)} (срок {fmt_date(o.effective_due, uow.today)})" for o in late
        )
        parts.append(f"Срок прошёл, отметки нет: {items}.")
    return " ".join(parts)


async def pick_current_occurrence(
    uow: Uow, owner_type: str, owner_id: str, title: str
) -> Occurrence:
    """«Current» period: the single open one due by today, else the next upcoming one."""
    rows = await uow.open_occurrences(owner_type, owner_id)
    due_now = [o for o in rows if o.effective_due <= uow.today]
    if len(due_now) == 1:
        return due_now[0]
    if len(due_now) > 1:
        raise NeedsClarification(
            f"У «{title}» несколько открытых периодов. Какой имеешь в виду?",
            code="ambiguous_period",
            options=[
                {
                    "occurrence_id": o.id,
                    "label": period_text(uow, o),
                    "due": o.effective_due.isoformat(),
                }
                for o in due_now
            ],
        )
    if not rows:
        raise InvalidState(f"У «{title}» нет открытых периодов", code="no_open_period")
    return rows[0]


# --- tasks ----------------------------------------------------------------------------


async def create_task(uow: Uow, cmd: c.CreateTask) -> Outcome:
    if cmd.due_time is not None and cmd.due_date is None:
        raise ValidationFailed("Время без даты не задать", code="time_without_date")
    if cmd.recurrence is not None and cmd.due_date is not None:
        raise ValidationFailed("Для повтора дата задаётся правилом", code="due_and_recurrence")
    task = Task(
        title=cmd.title,
        note=cmd.note,
        url=cmd.url,
        next_action=cmd.next_action,
        status="open",
        created_at=uow.now,
        updated_at=uow.now,
    )
    if cmd.due_date is not None and cmd.due_time is not None:
        task.due_at = local_to_utc(cmd.due_date, cmd.due_time, uow.tz)
    else:
        task.due_date = cmd.due_date
    uow.session.add(task)
    await uow.session.flush()
    if cmd.recurrence is not None:
        recurrence = await create_recurrence(uow, "task", task.id, cmd.recurrence)
        task.recurrence_id = recurrence.id
    await uow.audit(task, "create", None)
    uow.mark_replan("task", task.id)
    await apply_replans(uow)

    text = f"Записал дело «{task.title}»"
    if task.recurrence_id is not None:
        recurrence = await uow.get(Recurrence, task.recurrence_id)
        text += f", {FREQUENCY_TEXT[recurrence.frequency]}. "
        text += await occurrence_summary(uow, "task", task.id)
    elif task.due_at is None and task.due_date is None:
        text += ". Срок не указан — не придумываю."
    else:
        text += f": {task_due_text(uow, task)}."
    if task.next_action:
        text += f" Следующий шаг: {task.next_action}."
    text += " " + await reminder_line(uow, "task", task.id)
    return Outcome(text, _task_data(task))


def _task_data(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "version": task.version,
        "status": task.status,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "next_action": task.next_action,
    }


async def update_task(uow: Uow, cmd: c.UpdateTask) -> Outcome:
    task = await uow.get_versioned(Task, cmd.task_id, cmd.expected_version)
    if task.status != "open":
        raise InvalidState(f"«{task.title}» уже закрыто", code="task_closed")
    fields = cmd.model_fields_set - {"type", "task_id", "expected_version"}
    if not fields:
        raise ValidationFailed("Нечего менять", code="empty_update")
    before = snapshot(task)
    changes: list[str] = []
    undoable = True

    for name in ("title", "note", "url", "next_action"):
        if name in fields:
            value = getattr(cmd, name)
            if name == "title" and value is None:
                raise ValidationFailed("Название не может быть пустым", code="empty_title")
            setattr(task, name, value)
    if "next_action" in fields:
        changes.append(
            f"следующий шаг — {task.next_action}" if task.next_action else "следующий шаг убран"
        )
    if "title" in fields:
        changes.append(f"название — «{task.title}»")
    if "note" in fields:
        changes.append("заметка обновлена")
    if "url" in fields:
        changes.append("ссылка обновлена")

    if fields & {"due_date", "due_time"}:
        if task.recurrence_id is not None:
            raise ValidationFailed(
                "У повторяющегося дела переносится конкретный период или меняется правило",
                code="recurring_due",
            )
        current_date = (
            task.due_at.astimezone(uow.tz).date() if task.due_at is not None else task.due_date
        )
        current_time = task.due_at.astimezone(uow.tz).time() if task.due_at is not None else None
        new_date = cmd.due_date if "due_date" in fields else current_date
        new_time = cmd.due_time if "due_time" in fields else current_time
        if new_date is None and new_time is not None:
            raise ValidationFailed("Время без даты не задать", code="time_without_date")
        if new_date is not None and new_time is not None:
            task.due_at = local_to_utc(new_date, new_time, uow.tz)
            task.due_date = None
        else:
            task.due_at = None
            task.due_date = new_date
        changes.append(task_due_text(uow, task))
        uow.mark_replan("task", task.id)

    if "recurrence" in fields:
        if task.recurrence_id is None or cmd.recurrence is None:
            raise ValidationFailed(
                "Превратить разовое дело в повторяющееся (и обратно) правкой нельзя",
                code="recurrence_kind_change",
            )
        recurrence = await uow.get(Recurrence, task.recurrence_id)
        await apply_rule_change(uow, recurrence, rule_from_spec(cmd.recurrence))
        recurrence.due_time = cmd.recurrence.due_time
        changes.append("правило повтора изменено для будущих периодов")
        uow.mark_replan("task", task.id)
        undoable = False

    uow.touch(task)
    await uow.audit(task, "update", before)
    await apply_replans(uow)
    text = f"Обновил «{task.title}»: " + "; ".join(changes) + "."
    if not fields & {"due_date", "due_time", "recurrence"}:
        text += f" Не закрыто, {task_due_text(uow, task)} без изменений."
    return Outcome(text, _task_data(task), undoable=undoable)


async def complete_task(uow: Uow, cmd: c.CompleteTask) -> Outcome:
    task = await uow.get_versioned(Task, cmd.task_id, cmd.expected_version)
    if task.status != "open":
        raise InvalidState(f"«{task.title}» уже закрыто", code="task_closed")
    if task.recurrence_id is not None:
        if cmd.occurrence_id is not None:
            occ = await uow.get_versioned(Occurrence, cmd.occurrence_id, cmd.occurrence_version)
            if occ.owner_id != task.id:
                raise ValidationFailed("Период не от этого дела", code="foreign_occurrence")
        else:
            occ = await pick_current_occurrence(uow, "task", task.id, task.title)
        if occ.status != "open":
            raise InvalidState("Этот период уже закрыт", code="occurrence_closed")
        before = snapshot(occ)
        occ.status = "done"
        occ.resolved_at = uow.now
        uow.touch(occ)
        await uow.audit(occ, "update", before, parent_id=task.id)
        uow.mark_replan("task", task.id)
        await apply_replans(uow)
        text = f"Отметил «{task.title}» за {period_text(uow, occ)}. "
        text += await occurrence_summary(uow, "task", task.id)
        return Outcome(text.strip(), {**_task_data(task), "occurrence_id": occ.id})

    before = snapshot(task)
    task.status = "completed"
    task.completed_at = uow.now
    uow.touch(task)
    await uow.audit(task, "update", before)
    uow.mark_replan("task", task.id)
    await apply_replans(uow)
    return Outcome(
        f"Готово: «{task.title}» выполнено. Запланированные напоминания отменены.",
        _task_data(task),
    )


async def cancel_task(uow: Uow, cmd: c.CancelTask) -> Outcome:
    task = await uow.get_versioned(Task, cmd.task_id, cmd.expected_version)
    if task.status != "open":
        raise InvalidState(f"«{task.title}» уже закрыто", code="task_closed")
    before = snapshot(task)
    task.status = "cancelled"
    task.cancelled_at = uow.now
    uow.touch(task)
    for occ in await uow.open_occurrences("task", task.id):
        occ_before = snapshot(occ)
        occ.status = "cancelled"
        occ.resolved_at = uow.now
        uow.touch(occ)
        await uow.audit(occ, "update", occ_before, parent_id=task.id)
    await uow.audit(task, "update", before)
    uow.mark_replan("task", task.id)
    await apply_replans(uow)
    return Outcome(
        f"Отменил дело «{task.title}». История сохранена, напоминания сняты.", _task_data(task)
    )


async def _occurrence_owner(uow: Uow, occ: Occurrence) -> Task | Obligation:
    model = Task if occ.owner_type == "task" else Obligation
    return await uow.get(model, occ.owner_id)


async def reschedule_occurrence(uow: Uow, cmd: c.RescheduleOccurrence) -> Outcome:
    occ = await uow.get_versioned(Occurrence, cmd.occurrence_id, cmd.expected_version)
    if occ.status != "open":
        raise InvalidState("Перенести можно только открытый период", code="occurrence_closed")
    owner = await _occurrence_owner(uow, occ)
    before = snapshot(occ)
    occ.effective_due = cmd.new_due
    uow.touch(occ)
    await uow.audit(occ, "update", before, parent_id=owner.id)
    uow.mark_replan(occ.owner_type, owner.id)
    await apply_replans(uow)
    return Outcome(
        f"Перенёс «{owner.title}» за {period_text(uow, occ)} на "
        f"{fmt_date(occ.effective_due, uow.today)}. Правило повтора не менял.",
        {
            "occurrence_id": occ.id,
            "version": occ.version,
            "effective_due": occ.effective_due.isoformat(),
        },
    )


async def skip_occurrence(uow: Uow, cmd: c.SkipOccurrence) -> Outcome:
    occ = await uow.get_versioned(Occurrence, cmd.occurrence_id, cmd.expected_version)
    if occ.status != "open":
        raise InvalidState("Пропустить можно только открытый период", code="occurrence_closed")
    owner = await _occurrence_owner(uow, occ)
    before = snapshot(occ)
    occ.status = "skipped"
    occ.resolved_at = uow.now
    uow.touch(occ)
    await uow.audit(occ, "update", before, parent_id=owner.id)
    uow.mark_replan(occ.owner_type, owner.id)
    await apply_replans(uow)
    text = f"Пропустил «{owner.title}» за {period_text(uow, occ)}. "
    text += await occurrence_summary(uow, occ.owner_type, owner.id)
    return Outcome(text.strip(), {"occurrence_id": occ.id, "status": occ.status})


# --- obligations ----------------------------------------------------------------------


def _obligation_data(ob: Obligation) -> dict[str, Any]:
    return {
        "obligation_id": ob.id,
        "version": ob.version,
        "kind": ob.kind,
        "active": ob.active,
        "coverage_until": ob.coverage_until.isoformat() if ob.coverage_until else None,
    }


async def _forecast_line(uow: Uow, ob: Obligation) -> str:
    """Forecast (always marked as an estimate) plus the warning plan; after replans."""
    fc = await load_forecast(uow, ob)
    text = forecast_text(fc, ob.currency, uow.tz, uow.today)
    plan = await balance_plan_line(uow, ob, fc)
    return f"{text} {plan}".strip()


async def create_obligation(uow: Uow, cmd: c.CreateObligation) -> Outcome:
    currency = check_currency(cmd.currency)
    if cmd.kind == "calendar" and cmd.recurrence is None:
        raise ValidationFailed(
            "Для календарного платежа нужно правило повтора", code="recurrence_required"
        )
    if cmd.kind != "calendar" and cmd.recurrence is not None:
        raise ValidationFailed(
            "Правило повтора только для календарного платежа", code="unexpected_recurrence"
        )
    if cmd.kind != "coverage" and cmd.coverage_until is not None:
        raise ValidationFailed(
            "Граница оплаты только для периода покрытия", code="unexpected_coverage"
        )
    if cmd.kind != "balance" and cmd.balance is not None:
        raise ValidationFailed("Остаток и расход только для баланса", code="unexpected_balance")
    if cmd.duplicates == "forbid":
        twin = await find_duplicate(uow, cmd.title)
        if twin is not None:
            raise VersionConflict(
                f"«{twin.title}» уже есть — ничего не применил. Напиши, что добавить, ещё раз.",
                code="duplicate_obligation",
            )
    ob = Obligation(
        title=cmd.title,
        aliases=list(cmd.aliases),
        kind=cmd.kind,
        currency=currency,
        amount_minor=cmd.amount_minor,
        note=cmd.note,
        active=True,
        coverage_until=cmd.coverage_until,
        created_at=uow.now,
        updated_at=uow.now,
    )
    uow.session.add(ob)
    await uow.session.flush()
    await uow.audit(ob, "create", None)
    if cmd.recurrence is not None:
        recurrence = await create_recurrence(uow, "obligation", ob.id, cmd.recurrence)
        ob.recurrence_id = recurrence.id
    if cmd.balance is not None:
        start = cmd.balance
        at = check_not_future(uow, start.checkpoint_at)
        if start.checkpoint_minor is not None and start.assume_zero:
            raise ValidationFailed("Либо остаток, либо «считать от нуля»", code="bad_baseline")
        if start.checkpoint_minor is not None:
            await add_event(uow, ob, "correction", at, amount_minor=start.checkpoint_minor)
        elif start.assume_zero:
            await add_event(uow, ob, "correction", at, amount_minor=0, assumed=True)
        if start.rate_minor_per_day is not None:
            await add_event(uow, ob, "rate_change", at, rate_minor_per_day=start.rate_minor_per_day)
    uow.mark_replan("obligation", ob.id)
    await apply_replans(uow)

    text = f"Завёл «{ob.title}». "
    if ob.kind == "calendar":
        text += await occurrence_summary(uow, "obligation", ob.id)
    elif ob.kind == "coverage":
        text += (
            coverage_text(ob.coverage_until, uow.today) + "."
            if ob.coverage_until
            else "Граница оплаты пока неизвестна."
        )
    else:
        fc = await load_forecast(uow, ob)
        text += forecast_text(fc, ob.currency, uow.tz, uow.today)
        plan = await balance_plan_line(uow, ob, fc)
        if plan:
            return Outcome(f"{text.strip()} {plan}", _obligation_data(ob))
    text = text.strip() + " " + await reminder_line(uow, "obligation", ob.id)
    return Outcome(text, _obligation_data(ob))


async def update_obligation(uow: Uow, cmd: c.UpdateObligation) -> Outcome:
    ob = await uow.get_versioned(Obligation, cmd.obligation_id, cmd.expected_version)
    if not ob.active:
        raise InvalidState(f"«{ob.title}» в архиве", code="archived")
    fields = cmd.model_fields_set - {"type", "obligation_id", "expected_version"}
    if not fields:
        raise ValidationFailed("Нечего менять", code="empty_update")
    before = snapshot(ob)
    undoable = True
    for name in ("title", "aliases", "amount_minor", "note"):
        if name in fields:
            value = getattr(cmd, name)
            if name == "title" and value is None:
                raise ValidationFailed("Название не может быть пустым", code="empty_title")
            setattr(ob, name, list(value) if name == "aliases" and value else value)
    if "recurrence" in fields:
        if ob.kind != "calendar" or ob.recurrence_id is None or cmd.recurrence is None:
            raise ValidationFailed(
                "Правило повтора есть только у календарного платежа", code="unexpected_recurrence"
            )
        recurrence = await uow.get(Recurrence, ob.recurrence_id)
        await apply_rule_change(uow, recurrence, rule_from_spec(cmd.recurrence))
        uow.mark_replan("obligation", ob.id)
        undoable = False
    uow.touch(ob)
    await uow.audit(ob, "update", before)
    await apply_replans(uow)
    text = f"Обновил «{ob.title}»."
    if "recurrence" in fields:
        text += " Новое правило касается будущих открытых периодов; оплаченная история прежняя. "
        text += await occurrence_summary(uow, "obligation", ob.id)
    return Outcome(text.strip(), _obligation_data(ob), undoable=undoable)


async def mark_payment(uow: Uow, cmd: c.MarkPayment) -> Outcome:
    ob = await uow.get(Obligation, cmd.obligation_id)
    if ob.kind != "calendar":
        raise ValidationFailed(
            "Отметка периода — для календарного платежа. Для баланса это пополнение, "
            "для покрытия — продление.",
            code="wrong_kind",
        )
    if cmd.amount_minor is not None and len(cmd.occurrences) > 1:
        raise ValidationFailed("Сумму указывай для одного периода", code="amount_for_many")
    paid_at = check_not_future(uow, cmd.paid_at)
    ids = [ref.id for ref in cmd.occurrences]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("Период указан дважды", code="duplicate_period")
    paid: list[Occurrence] = []
    for ref in cmd.occurrences:
        occ = await uow.get_versioned(Occurrence, ref.id, ref.expected_version)
        if occ.owner_type != "obligation" or occ.owner_id != ob.id:
            raise ValidationFailed("Период не от этого платежа", code="foreign_occurrence")
        if occ.status != "open":
            raise InvalidState(
                f"{period_text(uow, occ).capitalize()} уже отмечен ({occ.status})",
                code="occurrence_closed",
            )
        before = snapshot(occ)
        occ.status = "paid"
        occ.resolved_at = paid_at
        uow.touch(occ)
        await uow.audit(occ, "update", before, parent_id=ob.id)
        payment = Payment(
            obligation_id=ob.id,
            occurrence_id=occ.id,
            paid_at=paid_at,
            amount_minor=cmd.amount_minor,
            currency=ob.currency,
            command_id=uow.audit_command_id,
            created_at=uow.now,
        )
        uow.session.add(payment)
        await uow.audit(payment, "create", None, parent_id=ob.id)
        paid.append(occ)
    uow.mark_replan("obligation", ob.id)
    await apply_replans(uow)
    periods = ", ".join(period_text(uow, o) for o in paid)
    text = f"Отметил оплату «{ob.title}»: {periods}"
    if cmd.amount_minor is not None:
        text += f", {fmt_money(cmd.amount_minor, ob.currency)}"
    text += ". " + await occurrence_summary(uow, "obligation", ob.id)
    return Outcome(
        text.strip(),
        {**_obligation_data(ob), "paid_occurrence_ids": [o.id for o in paid]},
    )


async def extend_coverage_handler(uow: Uow, cmd: c.ExtendCoverage) -> Outcome:
    ob = await uow.get_versioned(Obligation, cmd.obligation_id, cmd.expected_version)
    if ob.kind != "coverage":
        raise ValidationFailed("Продление — для оплаченных периодов (coverage)", code="wrong_kind")
    if not ob.active:
        raise InvalidState(f"«{ob.title}» в архиве", code="archived")
    paid_at = check_not_future(uow, cmd.paid_at)
    current = ob.coverage_until
    if cmd.until is not None:
        if cmd.unit is not None or cmd.count is not None:
            raise ValidationFailed("Либо срок, либо длительность", code="bad_coverage")
        start = cmd.start or current
        new_until = cmd.until
    else:
        if cmd.unit is None or cmd.count is None:
            raise ValidationFailed("Укажи длительность продления", code="bad_coverage")
        if cmd.start is not None:
            start = cmd.start
        elif current is None:
            raise NeedsClarification(
                "Не знаю, до какого дня было оплачено. С какой даты считать продление?",
                code="coverage_start_unknown",
            )
        elif current <= uow.today:
            raise NeedsClarification(
                f"Оплата истекла: последний оплаченный день — "
                f"{fmt_date(paid_through(current), uow.today)}. Считать продление от "
                f"{fmt_date(current, uow.today)} или от сегодня? Провайдеры считают по-разному.",
                code="coverage_expired",
                options=[
                    {"start": current.isoformat(), "label": "от конца прошлой оплаты"},
                    {"start": uow.today.isoformat(), "label": "от сегодня"},
                ],
            )
        else:
            start = current
        new_until = extend_coverage(start, cmd.unit, cmd.count)
    if current is not None and new_until <= current:
        raise ValidationFailed(
            f"Новая граница {fmt_date(new_until, uow.today)} не позже текущей "
            f"{fmt_date(current, uow.today)}",
            code="coverage_not_extended",
        )
    before = snapshot(ob)
    ob.coverage_until = new_until
    uow.touch(ob)
    await uow.audit(ob, "update", before)
    payment = Payment(
        obligation_id=ob.id,
        paid_at=paid_at,
        amount_minor=cmd.amount_minor,
        currency=ob.currency,
        coverage_from=start,
        coverage_until=new_until,
        command_id=uow.audit_command_id,
        created_at=uow.now,
    )
    uow.session.add(payment)
    await uow.audit(payment, "create", None, parent_id=ob.id)
    uow.mark_replan("obligation", ob.id)
    await apply_replans(uow)
    text = f"«{ob.title}»: {coverage_text(new_until, uow.today)}."
    return Outcome(
        text, {**_obligation_data(ob), "coverage_from": start.isoformat() if start else None}
    )


async def _balance_obligation(uow: Uow, obligation_id: str) -> Obligation:
    ob = await uow.get(Obligation, obligation_id)
    if ob.kind != "balance":
        raise ValidationFailed("Это не обязательство с балансом", code="wrong_kind")
    if not ob.active:
        raise InvalidState(f"«{ob.title}» в архиве", code="archived")
    return ob


async def record_topup(uow: Uow, cmd: c.RecordTopup) -> Outcome:
    ob = await _balance_obligation(uow, cmd.obligation_id)
    at = check_not_future(uow, cmd.effective_at)
    state = await estimate_before(uow, ob.id, at)
    amount = fmt_money(cmd.amount_minor, ob.currency)
    if state.estimate is None and not cmd.start_from_zero:
        raise NeedsClarification(
            f"Записать пополнение {amount} могу, но без стартового остатка прогноз не "
            "посчитать. Сколько было до пополнения? Или считать, что было 0?",
            code="baseline_unknown",
            options=[{"start_from_zero": True, "label": "считать от нуля"}],
        )
    if state.estimate is not None and state.estimate < 0 and not cmd.start_from_zero:
        raise NeedsClarification(
            "По расчёту запас уже был исчерпан, а продолжалось ли списание — неизвестно. "
            "Чтобы не вычитать воображаемый долг из пополнения: считать, что до пополнения "
            "было 0, или укажешь фактический остаток?",
            code="topup_after_depletion",
            options=[{"start_from_zero": True, "label": "до пополнения было 0"}],
        )
    if cmd.start_from_zero:
        await add_event(uow, ob, "correction", at, amount_minor=0, assumed=True)
    await add_event(uow, ob, "topup", at, amount_minor=cmd.amount_minor)
    await apply_replans(uow)
    text = f"Записал пополнение «{ob.title}» на {amount} ({fmt_instant(at, uow.tz, uow.today)}). "
    text += await _forecast_line(uow, ob)
    return Outcome(text, await _balance_data(uow, ob))


async def _balance_data(uow: Uow, ob: Obligation) -> dict[str, Any]:
    fc = await load_forecast(uow, ob)
    return {
        **_obligation_data(ob),
        "forecast_status": fc.status,
        "estimate_minor": str(fc.estimate_minor) if fc.estimate_minor is not None else None,
        "days_left": fc.days_left_floor,
        "depletion_at": fc.depletion_at.isoformat() if fc.depletion_at else None,
        "threshold_at": fc.threshold_at.isoformat() if fc.threshold_at else None,
    }


async def correct_balance(uow: Uow, cmd: c.CorrectBalance) -> Outcome:
    ob = await _balance_obligation(uow, cmd.obligation_id)
    at = check_not_future(uow, cmd.effective_at)
    await add_event(uow, ob, "correction", at, amount_minor=cmd.amount_minor, assumed=cmd.assumed)
    await apply_replans(uow)
    text = (
        f"Сверка «{ob.title}»: {fmt_money(cmd.amount_minor, ob.currency)} на "
        f"{fmt_instant(at, uow.tz, uow.today)}. "
    )
    text += await _forecast_line(uow, ob)
    return Outcome(text, await _balance_data(uow, ob))


async def change_daily_rate(uow: Uow, cmd: c.ChangeDailyRate) -> Outcome:
    if cmd.rate_minor_per_day < 0:
        raise ValidationFailed(
            "Расход в день не может быть отрицательным. Возврат или кредит запиши "
            "пополнением или сверкой остатка.",
            code="negative_rate",
        )
    ob = await _balance_obligation(uow, cmd.obligation_id)
    at = check_not_future(uow, cmd.effective_at)
    await add_event(uow, ob, "rate_change", at, rate_minor_per_day=cmd.rate_minor_per_day)
    await apply_replans(uow)
    text = (
        f"Расход «{ob.title}»: {fmt_money(cmd.rate_minor_per_day, ob.currency)}/день с "
        f"{fmt_instant(at, uow.tz, uow.today)}. "
    )
    text += await _forecast_line(uow, ob)
    return Outcome(text, await _balance_data(uow, ob))


async def archive_obligation(uow: Uow, cmd: c.ArchiveObligation) -> Outcome:
    ob = await uow.get_versioned(Obligation, cmd.obligation_id, cmd.expected_version)
    if not ob.active:
        raise InvalidState(f"«{ob.title}» уже в архиве", code="archived")
    late = await outstanding(uow, "obligation", ob.id)
    if late and cmd.outstanding is None:
        items = ", ".join(period_text(uow, o) for o in late)
        raise NeedsClarification(
            f"У «{ob.title}» есть прошлые периоды без отметки об оплате: {items}. "
            "Оставить их открытыми или закрыть без оплаты?",
            code="outstanding_choice",
            options=[
                {"outstanding": "keep_open", "label": "оставить открытыми"},
                {"outstanding": "close", "label": "закрыть без оплаты"},
            ],
        )
    late_ids = {o.id for o in late}
    for occ in await uow.open_occurrences("obligation", ob.id):
        if occ.id in late_ids and cmd.outstanding == "keep_open":
            continue
        before = snapshot(occ)
        occ.status = "cancelled"
        occ.resolved_at = uow.now
        uow.touch(occ)
        await uow.audit(occ, "update", before, parent_id=ob.id)
    before = snapshot(ob)
    ob.active = False
    ob.archived_at = uow.now
    uow.touch(ob)
    await uow.audit(ob, "update", before)
    uow.mark_replan("obligation", ob.id)
    await apply_replans(uow)
    text = (
        f"Убрал «{ob.title}» в архив: будущие периоды и напоминания больше не планирую, "
        "история платежей сохранена."
    )
    if late and cmd.outstanding == "keep_open":
        text += " Прошлые неоплаченные периоды оставил открытыми."
    return Outcome(text, _obligation_data(ob))


# --- notifications and settings -------------------------------------------------------


async def snooze_notification(uow: Uow, cmd: c.SnoozeNotification) -> Outcome:
    if cmd.until <= uow.now:
        raise ValidationFailed("Напомнить в прошлом не получится", code="snooze_in_past")
    subject: Task | Obligation
    if cmd.subject_type == "task":
        subject = await uow.get(Task, cmd.subject_id)
        if subject.status != "open":
            raise InvalidState(f"«{subject.title}» уже закрыто", code="task_closed")
    else:
        subject = await uow.get(Obligation, cmd.subject_id)
        if not subject.active:
            raise InvalidState(f"«{subject.title}» в архиве", code="archived")
    occurrence = None
    if cmd.occurrence_id is not None:
        occurrence = await uow.get(Occurrence, cmd.occurrence_id)
        if occurrence.owner_id != subject.id or occurrence.status != "open":
            raise ValidationFailed("Период не подходит для напоминания", code="bad_occurrence")
    job = await snooze(uow, subject, cmd.subject_type, occurrence, cmd.until, explicit=cmd.explicit)
    if isinstance(subject, Task):
        due = task_due_text(uow, subject)
    elif occurrence is not None:
        due = f"срок {fmt_date(occurrence.effective_due, uow.today)}"
    else:
        due = "срок прежний"
    text = (
        f"Ок, напомню про «{subject.title}» {fmt_instant(job.scheduled_at, uow.tz, uow.today)}. "
        f"Срок не трогаю: {due}."
    )
    return Outcome(
        text, {"job_id": job.id, "scheduled_at": job.scheduled_at.isoformat()}, undoable=False
    )


async def change_timezone(uow: Uow, cmd: c.ChangeTimezone) -> Outcome:
    settings = uow.settings
    if settings.version != cmd.expected_version:
        raise VersionConflict("Настройки изменились, обнови данные", code="version_conflict")
    get_zone(cmd.timezone)
    old = settings.timezone
    if old == cmd.timezone:
        raise ValidationFailed("Этот часовой пояс уже установлен", code="same_timezone")
    before = snapshot(settings)
    settings.timezone = cmd.timezone
    settings.version += 1
    settings.updated_at = uow.now
    await uow.audit(settings, "update", before, parent_id="settings")
    tasks = await uow.session.scalars(select(Task.id).where(Task.status == "open"))
    obligations = await uow.session.scalars(select(Obligation.id).where(Obligation.active))
    subjects = [("task", t) for t in tasks] + [("obligation", o) for o in obligations]
    for subject in subjects:
        uow.mark_replan(*subject)
    await apply_replans(uow)
    return Outcome(
        f"Часовой пояс: {old} → {cmd.timezone}. Будущие напоминания пересчитаны "
        "(дела с точным временем сохраняют свой момент); история не менялась.",
        {"timezone": cmd.timezone, "version": settings.version, "replanned": len(subjects)},
        undoable=False,
    )


async def update_settings(uow: Uow, cmd: c.UpdateSettings) -> Outcome:
    settings = uow.settings
    if settings.version != cmd.expected_version:
        raise VersionConflict("Настройки изменились, обнови данные", code="version_conflict")
    fields = {
        name: getattr(cmd, name)
        for name in cmd.model_fields_set - {"type", "expected_version"}
        if getattr(cmd, name) is not None
    }
    changed = {k: v for k, v in fields.items() if getattr(settings, k) != v}
    if not changed:
        return Outcome("Настройки не изменились.", {"version": settings.version}, undoable=False)
    before = snapshot(settings)
    for name, value in changed.items():
        setattr(settings, name, value)
    settings.version += 1
    settings.updated_at = uow.now
    await uow.audit(settings, "update", before, parent_id="settings")
    tasks = await uow.session.scalars(select(Task.id).where(Task.status == "open"))
    obligations = await uow.session.scalars(select(Obligation.id).where(Obligation.active))
    for subject in [("task", t) for t in tasks] + [("obligation", o) for o in obligations]:
        uow.mark_replan(*subject)
    await apply_replans(uow)
    return Outcome(
        "Настройки сохранены; будущие напоминания пересчитаны. Сроки дел не менялись.",
        {"version": settings.version, "changed": sorted(changed)},
        undoable=False,
    )


# --- undo -----------------------------------------------------------------------------


def _subject_of(entity_type: str, entity: Any) -> tuple[str, str] | None:
    if entity_type in ("task", "obligation"):
        return (entity_type, entity.id)
    if entity_type == "occurrence":
        return (entity.owner_type, entity.owner_id)
    if entity_type in ("payment", "balance_event"):
        return ("obligation", entity.obligation_id)
    return None


async def _last_undoable(uow: Uow) -> CommandReceipt:
    command_id = await uow.session.scalar(
        select(AuditEvent.command_id)
        .join(CommandReceipt, CommandReceipt.command_id == AuditEvent.command_id)
        .where(
            CommandReceipt.status == "applied",
            CommandReceipt.undoable,
            CommandReceipt.undone_by.is_(None),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    if command_id is None:
        raise UndoUnavailable("Нечего отменять", code="nothing_to_undo")
    return await uow.get(CommandReceipt, command_id)


async def undo_command(uow: Uow, cmd: c.UndoCommand) -> Outcome:
    if cmd.target_command_id is not None:
        target = await uow.session.get(CommandReceipt, cmd.target_command_id)
        if target is None:
            raise UndoUnavailable("Такой команды не было", code="unknown_command")
    else:
        target = await _last_undoable(uow)
    if target.undone_by is not None:
        raise UndoUnavailable("Это действие уже отменено", code="already_undone")
    if target.status != "applied" or not target.undoable:
        raise UndoUnavailable(
            "Это действие нельзя отменить автоматически; поправь данные явно.",
            code="not_undoable",
        )
    events = list(
        await uow.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.command_id == target.command_id)
            .order_by(AuditEvent.id)
        )
    )
    if not events:
        raise UndoUnavailable("У действия нет изменений для отмены", code="nothing_to_undo")
    last_id = events[-1].id
    ignored = select(CommandReceipt.command_id).where(
        or_(CommandReceipt.undone_by.is_not(None), CommandReceipt.command_type == "undo_command")
    )
    entities: list[tuple[AuditEvent, Any]] = []
    for event in events:
        targets = AuditEvent.entity_id == event.entity_id
        if event.action == "create" and event.entity_type in ("task", "obligation"):
            targets = or_(targets, AuditEvent.parent_id == event.entity_id)
        dependent = await uow.session.scalar(
            select(
                exists().where(
                    and_(AuditEvent.id > last_id, targets, AuditEvent.command_id.not_in(ignored))
                )
            )
        )
        model = MODELS_BY_TYPE[event.entity_type]
        entity = await uow.get(model, event.entity_id)
        changed = restorable_view(event.entity_type, snapshot(entity)) != restorable_view(
            event.entity_type, event.after or {}
        )
        if dependent or changed:
            raise UndoUnavailable(
                "После этого действия те же записи менялись ещё раз. Автоматически откатить "
                "нельзя — поправь нужное явно.",
                code="undo_conflict",
                entity_id=event.entity_id,
            )
        entities.append((event, entity))

    for event, entity in reversed(entities):
        before = snapshot(entity)
        if event.action == "create":
            await _revert_creation(uow, event.entity_type, entity)
        else:
            model = MODELS_BY_TYPE[event.entity_type]
            for name in RESTORABLE[event.entity_type]:
                setattr(entity, name, from_json(model, name, (event.before or {}).get(name)))
        uow.touch(entity)
        await uow.audit(
            entity,
            "undo",
            before,
            parent_id=event.parent_id,
            reason=f"undo:{target.command_id}"[:64],
        )
        subject = _subject_of(event.entity_type, entity)
        if subject is not None:
            uow.mark_replan(*subject)
    target.undone_by = uow.ctx.command_id
    await apply_replans(uow)
    original = (target.result or {}).get("message", "")
    return Outcome(
        f"Отменил предыдущее действие. Было: {original}".strip(),
        {"undone_command_id": target.command_id},
        undoable=False,
    )


async def _revert_creation(uow: Uow, entity_type: str, entity: Any) -> None:
    if entity_type == "task":
        entity.status = "cancelled"
        entity.cancelled_at = uow.now
        for occ in await uow.open_occurrences("task", entity.id):
            occ.status = "cancelled"
            occ.resolved_at = uow.now
            uow.touch(occ)
    elif entity_type == "obligation":
        entity.active = False
        entity.archived_at = uow.now
        for occ in await uow.open_occurrences("obligation", entity.id):
            occ.status = "cancelled"
            occ.resolved_at = uow.now
            uow.touch(occ)
    elif entity_type in ("payment", "balance_event"):
        entity.reverted_at = uow.now
        entity.reverted_by = uow.ctx.command_id
    else:
        raise UndoUnavailable("Это создание нельзя отменить", code="not_undoable")


# --- pending intents ------------------------------------------------------------------


async def confirm_pending(uow: Uow, cmd: c.ConfirmPendingIntent) -> Outcome:
    pending = await uow.get(PendingIntent, cmd.pending_id)
    _check_pending_usable(uow, pending)
    actions = [c.COMMAND_ADAPTER.validate_python(a) for a in pending.actions]
    outcomes: list[Outcome] = []
    try:
        for action in actions:
            outcomes.append(await dispatch(uow, action))
    except VersionConflict as exc:
        raise VersionConflict(
            "С момента предложения данные изменились — ничего не применил. Повтори запрос, "
            "покажу актуальный список.",
            code="stale_preview",
        ) from exc
    pending.status = "confirmed"
    pending.resolved_at = uow.now
    pending.resolved_by = uow.ctx.command_id
    lines = [f"{i}. {o.message}" for i, o in enumerate(outcomes, 1)]
    return Outcome(
        "Применил всё вместе:\n" + "\n".join(lines),
        {"pending_id": pending.id, "results": [o.data for o in outcomes]},
        undoable=all(o.undoable for o in outcomes),
    )


async def cancel_pending(uow: Uow, cmd: c.CancelPendingIntent) -> Outcome:
    pending = await uow.get(PendingIntent, cmd.pending_id)
    _check_pending_usable(uow, pending)
    pending.status = "cancelled"
    pending.resolved_at = uow.now
    pending.resolved_by = uow.ctx.command_id
    return Outcome("Ок, ничего не меняю.", {"pending_id": pending.id}, undoable=False)


def _check_pending_usable(uow: Uow, pending: PendingIntent) -> None:
    if pending.status == "superseded":
        raise InvalidState("Это предложение уже заменено более новым", code="pending_superseded")
    if pending.status != "pending":
        raise InvalidState("Это предложение уже обработано", code="pending_resolved")
    if pending.expires_at <= uow.now:
        raise InvalidState("Предложение устарело. Повтори запрос.", code="pending_expired")


# --- dispatch -------------------------------------------------------------------------

HANDLERS: dict[str, Handler] = {
    "create_task": create_task,
    "update_task": update_task,
    "complete_task": complete_task,
    "cancel_task": cancel_task,
    "reschedule_occurrence": reschedule_occurrence,
    "skip_occurrence": skip_occurrence,
    "create_obligation": create_obligation,
    "update_obligation": update_obligation,
    "mark_payment": mark_payment,
    "extend_coverage": extend_coverage_handler,
    "record_topup": record_topup,
    "correct_balance": correct_balance,
    "change_daily_rate": change_daily_rate,
    "archive_obligation": archive_obligation,
    "snooze_notification": snooze_notification,
    "change_timezone": change_timezone,
    "update_settings": update_settings,
    "undo_command": undo_command,
    "confirm_pending": confirm_pending,
    "cancel_pending": cancel_pending,
}


async def dispatch(uow: Uow, command: c.Command) -> Outcome:
    outcome = await HANDLERS[command.type](uow, command)
    # A confirmed batch is undoable when each of its actions is (decided in the handler).
    if command.type not in c.UNDOABLE_TYPES and command.type != "confirm_pending":
        outcome.undoable = False
    return outcome
