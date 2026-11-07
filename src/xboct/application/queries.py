"""Read models: agenda, payments, balance forecasts, candidates for the intent provider.

Overdue is derived from time and openness, never stored. Archived obligations keep
outstanding periods visible if the owner chose to keep them open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import select

from xboct.application.balance_ledger import load_forecast
from xboct.application.render import (
    coverage_text,
    fmt_date,
    fmt_instant,
    fmt_money,
    fmt_period,
    forecast_text,
)
from xboct.application.uow import Uow
from xboct.domain.balance import BalanceForecast
from xboct.domain.coverage import paid_through
from xboct.llm.contract import MAX_CANDIDATES, CandidateRecord
from xboct.storage.models import Obligation, Occurrence, Payment, Task

ItemKind = Literal["task", "task_occurrence", "payment_occurrence", "coverage"]


@dataclass(frozen=True)
class AgendaItem:
    kind: ItemKind
    subject_id: str
    title: str
    due_date: date
    due_at: datetime | None = None
    occurrence_id: str | None = None
    period: str | None = None
    next_action: str | None = None

    def text(self, uow: Uow) -> str:
        when = (
            fmt_instant(self.due_at, uow.tz, uow.today_now)
            if self.due_at is not None
            else fmt_date(self.due_date, uow.today_now)
        )
        label = f"«{self.title}»"
        if self.period:
            label += f" за {self.period}"
        line = f"{label} — {when}"
        if self.next_action:
            line += f"; следующий шаг: {self.next_action}"
        return line


@dataclass
class Agenda:
    today: date
    overdue: list[AgendaItem] = field(default_factory=list)
    due_today: list[AgendaItem] = field(default_factory=list)
    next_7_days: list[AgendaItem] = field(default_factory=list)
    undated: list[AgendaItem] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.overdue or self.due_today or self.next_7_days)


async def agenda(uow: Uow) -> Agenda:
    today = uow.today_now
    week_end = today + timedelta(days=7)
    result = Agenda(today=today)
    items: list[AgendaItem] = []

    for task in await uow.session.scalars(
        select(Task).where(Task.status == "open", Task.recurrence_id.is_(None))
    ):
        if task.due_at is not None:
            local_due = task.due_at.astimezone(uow.tz).date()
            items.append(
                AgendaItem(
                    "task",
                    task.id,
                    task.title,
                    local_due,
                    task.due_at,
                    next_action=task.next_action,
                )
            )
        elif task.due_date is not None:
            items.append(
                AgendaItem("task", task.id, task.title, task.due_date, next_action=task.next_action)
            )
        else:
            result.undated.append(
                AgendaItem("task", task.id, task.title, today, next_action=task.next_action)
            )

    occurrences = await uow.session.scalars(
        select(Occurrence).where(Occurrence.status == "open", Occurrence.effective_due <= week_end)
    )
    for occ in occurrences:
        if occ.owner_type == "task":
            owner = await uow.session.get(Task, occ.owner_id)
            if owner is None or owner.status != "open":
                continue
            items.append(
                AgendaItem(
                    "task_occurrence",
                    owner.id,
                    owner.title,
                    occ.effective_due,
                    occurrence_id=occ.id,
                    period=fmt_period(occ.period_key, occ.nominal_due, today),
                    next_action=owner.next_action,
                )
            )
        else:
            ob = await uow.session.get(Obligation, occ.owner_id)
            if ob is None:
                continue
            items.append(
                AgendaItem(
                    "payment_occurrence",
                    ob.id,
                    ob.title,
                    occ.effective_due,
                    occurrence_id=occ.id,
                    period=fmt_period(occ.period_key, occ.nominal_due, today),
                )
            )

    for ob in await uow.session.scalars(
        select(Obligation).where(
            Obligation.active, Obligation.kind == "coverage", Obligation.coverage_until.is_not(None)
        )
    ):
        assert ob.coverage_until is not None
        last_paid = paid_through(ob.coverage_until)
        if last_paid <= week_end:
            items.append(
                AgendaItem(
                    "coverage", ob.id, ob.title, last_paid, period="последний оплаченный день"
                )
            )

    for item in sorted(
        items,
        key=lambda i: (
            i.due_date,
            i.due_at or datetime.min.replace(tzinfo=uow.now.tzinfo),
            i.title,
        ),
    ):
        overdue = item.due_at < uow.now if item.due_at is not None else item.due_date < today
        if overdue:
            result.overdue.append(item)
        elif item.due_date == today:
            result.due_today.append(item)
        elif item.due_date <= week_end:
            result.next_7_days.append(item)
    return result


def agenda_text(uow: Uow, view: Agenda) -> str:
    if view.empty:
        return "На неделе спокойно." + (f" Без срока: {len(view.undated)}." if view.undated else "")
    blocks: list[str] = []
    if view.overdue:
        blocks.append(
            "Срок прошёл, отметки нет:\n" + "\n".join(f"• {i.text(uow)}" for i in view.overdue)
        )
    if view.due_today:
        blocks.append("Сегодня:\n" + "\n".join(f"• {i.text(uow)}" for i in view.due_today))
    if view.next_7_days:
        blocks.append(
            "Ближайшие 7 дней:\n" + "\n".join(f"• {i.text(uow)}" for i in view.next_7_days)
        )
    if view.undated:
        blocks.append(f"Без срока: {len(view.undated)}.")
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class PaymentView:
    obligation_id: str
    title: str
    kind: str
    currency: str
    active: bool
    next_due: date | None = None
    next_period: str | None = None
    outstanding: list[str] = field(default_factory=list)
    coverage_until: date | None = None
    forecast: BalanceForecast | None = None
    last_payment_at: datetime | None = None
    last_payment_minor: int | None = None


async def payments(uow: Uow, *, include_archived: bool = False) -> list[PaymentView]:
    today = uow.today_now
    query = select(Obligation).order_by(Obligation.title)
    if not include_archived:
        query = query.where(Obligation.active)
    views: list[PaymentView] = []
    for ob in await uow.session.scalars(query):
        last = await uow.session.scalar(
            select(Payment)
            .where(Payment.obligation_id == ob.id, Payment.reverted_at.is_(None))
            .order_by(Payment.paid_at.desc())
            .limit(1)
        )
        base = {
            "obligation_id": ob.id,
            "title": ob.title,
            "kind": ob.kind,
            "currency": ob.currency,
            "active": ob.active,
            "last_payment_at": last.paid_at if last else None,
            "last_payment_minor": last.amount_minor if last else None,
        }
        if ob.kind == "calendar":
            open_occ = await uow.open_occurrences("obligation", ob.id)
            upcoming = next((o for o in open_occ if o.effective_due >= today), None)
            late = [o for o in open_occ if o.effective_due < today]
            views.append(
                PaymentView(
                    **base,
                    next_due=upcoming.effective_due if upcoming else None,
                    next_period=fmt_period(upcoming.period_key, upcoming.nominal_due, today)
                    if upcoming
                    else None,
                    outstanding=[fmt_period(o.period_key, o.nominal_due, today) for o in late],
                )
            )
        elif ob.kind == "coverage":
            views.append(PaymentView(**base, coverage_until=ob.coverage_until))
        else:
            views.append(PaymentView(**base, forecast=await load_forecast(uow, ob)))
    return views


def payments_text(uow: Uow, views: list[PaymentView]) -> str:
    if not views:
        return "Платежей пока нет."
    lines: list[str] = []
    today = uow.today_now
    for v in views:
        if v.kind == "calendar":
            line = f"«{v.title}»: "
            line += (
                f"следующий срок {fmt_date(v.next_due, today)} ({v.next_period})"
                if v.next_due
                else "открытых периодов нет"
            )
            if v.outstanding:
                line += f"; срок прошёл, отметки нет: {', '.join(v.outstanding)}"
        elif v.kind == "coverage":
            line = f"«{v.title}»: " + (
                coverage_text(v.coverage_until, today)
                if v.coverage_until
                else "граница оплаты неизвестна"
            )
        else:
            assert v.forecast is not None
            line = f"«{v.title}»: {forecast_text(v.forecast, v.currency, uow.tz, today)}"
        if v.last_payment_at is not None:
            line += (
                f" Последняя оплата: {fmt_date(v.last_payment_at.astimezone(uow.tz).date(), today)}"
            )
            if v.last_payment_minor is not None:
                line += f", {fmt_money(v.last_payment_minor, v.currency)}"
            line += "."
        lines.append(line)
    return "\n".join(lines)


async def balance_forecasts(uow: Uow) -> list[tuple[Obligation, BalanceForecast]]:
    rows = await uow.session.scalars(
        select(Obligation)
        .where(Obligation.active, Obligation.kind == "balance")
        .order_by(Obligation.title)
    )
    return [(ob, await load_forecast(uow, ob)) for ob in rows]


# --- candidates for the provider ------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(text.casefold()) if len(w) >= 3}


async def candidates(uow: Uow, message: str, limit: int = MAX_CANDIDATES) -> list[CandidateRecord]:
    """Open tasks and active obligations, ranked by crude word-stem overlap.

    This only narrows what the provider sees; it never decides a target by itself.
    """
    words = _tokens(message)
    scored: list[tuple[int, str, CandidateRecord]] = []
    for task in await uow.session.scalars(select(Task).where(Task.status == "open")):
        record = CandidateRecord(
            id=task.id,
            kind="task",
            title=task.title,
            status=task.status,
            version=task.version,
            due=task.due_at.astimezone(uow.tz).date() if task.due_at else task.due_date,
        )
        scored.append((len(words & _tokens(task.title)), task.title, record))
    for ob in await uow.session.scalars(select(Obligation).where(Obligation.active)):
        names = " ".join([ob.title, *ob.aliases])
        record = CandidateRecord(
            id=ob.id,
            kind="obligation",
            title=ob.title,
            aliases=list(ob.aliases),
            obligation_kind=ob.kind,  # type: ignore[arg-type]
            status="active",
            version=ob.version,
            due=ob.coverage_until,
        )
        scored.append((len(words & _tokens(names)), ob.title, record))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [record for _, _, record in scored[:limit]]
