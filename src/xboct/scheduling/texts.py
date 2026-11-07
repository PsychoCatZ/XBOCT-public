"""Deterministic reminder texts and buttons, built from the current stored state.

Facts (dates, amounts, periods) only come from the database; coverage uses the shared
`coverage_reminder_text`. Wording says «срок прошёл, отметки нет», never «не оплачено».
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xboct.application.balance_ledger import load_forecast
from xboct.application.planning import FINAL_SUFFIX
from xboct.application.render import (
    coverage_reminder_text,
    fmt_date,
    fmt_instant,
    fmt_period,
    forecast_text,
)
from xboct.application.uow import Uow
from xboct.domain.timeutil import local_date
from xboct.storage.models import NotificationJob, Obligation, Occurrence, Task
from xboct.telegram.callbacks import Token
from xboct.telegram.transport import Button

TITLE_IN_BUTTON = 18


@dataclass
class Rendered:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)


def _short(title: str) -> str:
    return title if len(title) <= TITLE_IN_BUTTON else title[: TITLE_IN_BUTTON - 1] + "…"


async def render_job(uow: Uow, job: NotificationJob, *, label_buttons: bool = False) -> Rendered:
    today = uow.today_now
    occ = await uow.session.get(Occurrence, job.occurrence_id) if job.occurrence_id else None
    snooze = Button("Позже", Token("snooze", job.id).encode())

    if job.subject_type == "task":
        task = await uow.get(Task, job.subject_id)
        suffix = f" — {_short(task.title)}" if label_buttons else ""
        if occ is not None:
            done = Token("done_occ", occ.id, occ.version)
            due_text = fmt_date(occ.effective_due, today)
            what = f"«{task.title}» за {fmt_period(occ.period_key, occ.nominal_due, today)}"
        else:
            done = Token("done_task", task.id, task.version)
            due_text = (
                fmt_instant(task.due_at, uow.tz, today)
                if task.due_at is not None
                else fmt_date(task.due_date, today)
                if task.due_date is not None
                else "без срока"
            )
            what = f"«{task.title}»"
        if job.kind == "due":
            text = f"Срок сегодня: {what} ({due_text})."
        elif job.kind == "overdue":
            text = f"Срок прошёл, отметки нет: {what} (срок {due_text})."
        else:
            text = f"Напоминаю: {what}, срок {due_text}."
        if task.next_action:
            text += f" Следующий шаг: {task.next_action}."
        buttons = [
            [Button("Готово" + suffix, done.encode()), Button(snooze.text + suffix, snooze.data)]
        ]
        return Rendered(text, buttons)

    ob = await uow.get(Obligation, job.subject_id)
    suffix = f" — {_short(ob.title)}" if label_buttons else ""
    later = [Button(snooze.text + suffix, snooze.data)]
    if ob.kind == "coverage" and ob.coverage_until is not None and job.kind != "snooze":
        return Rendered(
            f"«{ob.title}»: " + coverage_reminder_text(job.kind, ob.coverage_until, today), [later]
        )
    if ob.kind == "balance":
        fc = await load_forecast(uow, ob)
        lead = "Последнее предупреждение: " if job.period_key.endswith(FINAL_SUFFIX) else ""
        return Rendered(
            f"{lead}«{ob.title}»: {forecast_text(fc, ob.currency, uow.tz, today)}", [later]
        )
    if occ is None:
        return Rendered(f"Напоминаю про «{ob.title}».", [later])
    period = fmt_period(occ.period_key, occ.nominal_due, today)
    due = fmt_date(occ.effective_due, today)
    if job.kind == "advance":
        days_left = (occ.effective_due - local_date(uow.now, uow.tz)).days
        text = f"Через {days_left} дн. платёж «{ob.title}» за {period} (срок {due})."
    elif job.kind == "due":
        text = f"Сегодня срок платежа «{ob.title}» за {period}."
    elif job.kind == "overdue":
        text = f"Срок платежа «{ob.title}» за {period} прошёл ({due}), отметки об оплате нет."
    else:
        text = f"Напоминаю: платёж «{ob.title}» за {period}, срок {due}."
    paid = Button("Оплачено" + suffix, Token("paid", occ.id, occ.version).encode())
    return Rendered(text, [[paid, *later]])


async def render_many(uow: Uow, jobs: list[NotificationJob], header: str) -> Rendered:
    parts = [await render_job(uow, job, label_buttons=True) for job in jobs]
    text = header + "\n" + "\n".join(f"• {p.text}" for p in parts)
    buttons = [row for p in parts for row in p.buttons][:20]
    return Rendered(text, buttons)
