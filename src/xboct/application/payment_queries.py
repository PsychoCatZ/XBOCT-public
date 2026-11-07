"""Payment questions retain their time window and distinguish due from paid history.

The model chooses typed filters; the application resolves calendar boundaries and reads
facts. Current coverage is not a claim that the next renewal has already been paid.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from xboct.application.balance_ledger import load_forecast
from xboct.application.queries import payments, payments_text
from xboct.application.render import fmt_date, fmt_money, fmt_period
from xboct.application.uow import Uow
from xboct.domain.coverage import paid_through
from xboct.domain.dates import next_week_range
from xboct.domain.timeutil import add_months, last_day_of_month
from xboct.llm.contract import QuerySpec
from xboct.storage.models import Obligation, Occurrence, Payment


def resolve_month_year(month: int, year: int | None, today: date, status: str) -> int:
    """Year of a month named without a year (an explicit year is always kept).

    Upcoming payments (unpaid/all): the nearest such month, the current one included.
    Payment history (paid): the latest such month, the current one included.
    The current paid status (covered) does not guess: the current year, as stated.
    """
    if year is not None:
        return year
    if status == "paid":
        return today.year if month <= today.month else today.year - 1
    if status == "covered":
        return today.year
    return today.year if month >= today.month else today.year + 1


def period_bounds(query: QuerySpec, today: date) -> tuple[date, date] | None:
    if query.period == "all":
        return None
    if query.period in ("today", "tomorrow"):
        day = today + timedelta(days=query.period == "tomorrow")
        return day, day
    if query.period == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if query.period == "next_week":
        return next_week_range(today)
    if query.period in ("this_month", "next_month", "month"):
        start = today.replace(day=1)
        if query.period == "next_month":
            start = add_months(start, 1)
        elif query.period == "month":
            assert query.month is not None
            year = resolve_month_year(query.month, query.year, today, query.payment_status)
            start = date(year, query.month, 1)
        return start, start.replace(day=last_day_of_month(start.year, start.month))
    assert query.start_date is not None
    assert query.end_date is not None
    return query.start_date, query.end_date


async def payment_query_text(uow: Uow, query: QuerySpec) -> str:
    if query.period == "all" and query.payment_status == "all":
        return payments_text(uow, await payments(uow))
    reference = uow.ctx.received_at.astimezone(uow.tz).date()
    bounds = period_bounds(query, reference)
    label = "" if bounds is None else f"с {bounds[0]:%d.%m.%Y} по {bounds[1]:%d.%m.%Y}"

    def inside(day: date) -> bool:
        return bounds is None or bounds[0] <= day <= bounds[1]

    if query.payment_status == "covered":
        return await covered_text(uow, bounds, reference)

    if query.payment_status == "paid":
        rows = []
        for payment in await uow.session.scalars(
            select(Payment).where(Payment.reverted_at.is_(None)).order_by(Payment.paid_at)
        ):
            day = payment.paid_at.astimezone(uow.tz).date()
            if not inside(day):
                continue
            ob = await uow.session.get(Obligation, payment.obligation_id)
            assert ob is not None
            line = f"• {fmt_date(day, reference)} — «{ob.title}»"
            if not ob.active:
                line += " (архивная запись)"
            if payment.occurrence_id is not None:
                occurrence = await uow.session.get(Occurrence, payment.occurrence_id)
                if occurrence is not None:
                    line += "; оплачено за " + fmt_period(
                        occurrence.period_key, occurrence.nominal_due, reference
                    )
            if payment.amount_minor is not None:
                line += f", {fmt_money(payment.amount_minor, payment.currency)}"
            if payment.coverage_until is not None:
                line += f"; оплачено по {fmt_date(paid_through(payment.coverage_until), reference)}"
            rows.append(line + ".")
        return (
            f"История оплат {label}:\n" + "\n".join(rows)
            if rows
            else f"В истории оплат {label} записей нет."
        )

    return await due_text(uow, query, bounds, label, reference)


OVERDUE = "срок прошёл, отметки оплаты нет"


async def due_text(
    uow: Uow, query: QuerySpec, bounds: tuple[date, date] | None, label: str, reference: date
) -> str:
    """Upcoming deadlines in the window; for unpaid, past-due items in their own block.

    Past-due items of active obligations (an open calendar period after its due date, a
    paid-through period that has ended) are listed for unpaid questions whatever the
    window: a new period does not close an old one. Each item appears in one block only.
    Paid, cancelled and archived records are not past-due. Not marked is not proof that
    nothing was paid, and an estimate is not a provider's balance.
    """
    unpaid = query.payment_status == "unpaid"

    def inside(day: date) -> bool:
        return bounds is None or bounds[0] <= day <= bounds[1]

    upcoming: list[tuple[date, str]] = []
    overdue: list[tuple[date, str]] = []
    unknown: list[str] = []
    stopped: list[str] = []

    def place(day: date, text: str, *, past_due: bool, short: str | None = None) -> None:
        """One block per item: past-due ones of an unpaid question go to their own block."""
        if past_due and unpaid:
            overdue.append((day, short or text))
        elif inside(day):
            upcoming.append((day, text))

    for ob in await uow.session.scalars(select(Obligation).where(Obligation.active)):
        if ob.kind == "calendar":
            statuses = ("open",) if unpaid else ("open", "paid")
            occurrences = list(
                await uow.session.scalars(
                    select(Occurrence)
                    .where(
                        Occurrence.owner_type == "obligation",
                        Occurrence.owner_id == ob.id,
                        Occurrence.status.in_(statuses),
                    )
                    .order_by(Occurrence.effective_due)
                )
            )
            if bounds is None:
                past = [o for o in occurrences if o.effective_due < reference]
                future = [o for o in occurrences if o.effective_due >= reference]
                occurrences = past + future[:1]
            for occurrence in occurrences:
                day = occurrence.effective_due
                period = fmt_period(occurrence.period_key, occurrence.nominal_due, reference)
                if occurrence.status == "paid":
                    status = "оплачено"
                elif day < reference:
                    status = OVERDUE
                else:
                    status = "оплата не отмечена"
                past_due = occurrence.status == "open" and day < reference
                place(
                    day,
                    f"«{ob.title}» за {period}: {status}",
                    past_due=past_due,
                    short=f"«{ob.title}» за {period}",
                )
        elif ob.kind == "coverage":
            if ob.coverage_until is None:
                unknown.append(ob.title)
                continue
            day = paid_through(ob.coverage_until)
            if day < reference:
                text = (
                    f"«{ob.title}»: {OVERDUE} (последний оплаченный день — "
                    f"{fmt_date(day, reference)})"
                )
                short = f"«{ob.title}»: последний оплаченный день — {fmt_date(day, reference)}"
                place(day, text, past_due=True, short=short)
            else:
                place(day, f"«{ob.title}»: продлить не позже этого дня", past_due=False)
        else:
            forecast = await load_forecast(uow, ob)
            if forecast.status == "stopped":
                stopped.append(ob.title)
                continue
            if forecast.status == "depleted":
                text = (
                    f"«{ob.title}»: расчётный запас исчерпан — пора пополнить "
                    "(оценка по расходу, не данные провайдера)"
                )
                assert forecast.depletion_at is not None
                day = forecast.depletion_at.astimezone(uow.tz).date()
                place(min(day, reference), text, past_due=True)
                continue
            if forecast.threshold_at is None:
                unknown.append(ob.title)
                continue
            day = max(reference, forecast.threshold_at.astimezone(uow.tz).date())
            place(day, f"«{ob.title}»: пополнить баланс (срок по расчётной оценке)", past_due=False)

    heading = (
        f"Сроки оплаты {label}" if not unpaid else f"Ещё потребуется оплатить {label}"
    ).strip()
    if upcoming:
        text = (
            heading
            + ":\n"
            + "\n".join(f"• {fmt_date(day, reference)} — {line}." for day, line in sorted(upcoming))
        )
    else:
        text = f"{heading}: записанных обязательств с таким сроком нет."
    if overdue:
        text += "\n\nСрок прошёл, отметки оплаты нет:\n" + "\n".join(
            f"• {fmt_date(day, reference)} — {line}." for day, line in sorted(overdue)
        )
    if stopped:
        text += "\nСписание остановлено (расход 0): " + ", ".join(sorted(stopped)) + "."
    if unknown:
        text += "\nНе могу определить срок для: " + ", ".join(sorted(unknown)) + "."
    return text


CALENDAR_MARKS = "Отмеченные календарные оплаты"
CALENDAR_NOTE = "Это отметки об оплате конкретных периодов; срок покрытия по ним не вычисляется."


async def covered_text(uow: Uow, bounds: tuple[date, date] | None, reference: date) -> str:
    """Current paid state, in two kinds of facts that are never mixed.

    coverage: the explicitly stored paid-through day, inclusive. Calendar payments: only
    the confirmed fact «оплата за <период> отмечена» (nominal and next due shown). What a
    mark covers — a quarter, a year — is not derived from the gap between nominal dates:
    without stored boundaries of coverage none are invented.
    """
    start, end = bounds or (reference, reference)
    start = max(start, reference)
    if start > end:
        return (
            "Для прошлого периода покажу историю оплат. "
            "Текущие сроки не восстанавливают прошлый статус."
        )
    coverage_rows: list[str] = []
    marked_rows: list[str] = []
    for ob in await uow.session.scalars(
        select(Obligation).where(Obligation.active).order_by(Obligation.title)
    ):
        if ob.kind == "coverage":
            if ob.coverage_until is not None and ob.coverage_until > start:
                coverage_rows.append(
                    f"• {ob.title} — оплачено по "
                    f"{fmt_date(paid_through(ob.coverage_until), reference)} включительно."
                )
        elif ob.kind == "calendar":
            paid = list(
                await uow.session.scalars(
                    select(Occurrence)
                    .where(
                        Occurrence.owner_type == "obligation",
                        Occurrence.owner_id == ob.id,
                        Occurrence.status == "paid",
                    )
                    .order_by(Occurrence.nominal_due)
                )
            )
            if bounds is not None:
                paid = [o for o in paid if bounds[0] <= o.nominal_due <= bounds[1]]
            elif paid:
                paid = paid[-1:]  # the latest mark; older ones are payment history
            if not paid:
                continue
            following = await uow.session.scalar(  # the next upcoming due, if one is stored
                select(Occurrence)
                .where(
                    Occurrence.owner_type == "obligation",
                    Occurrence.owner_id == ob.id,
                    Occurrence.status == "open",
                    Occurrence.effective_due >= reference,
                )
                .order_by(Occurrence.effective_due)
                .limit(1)
            )
            for occurrence in paid:
                period = fmt_period(occurrence.period_key, occurrence.nominal_due, reference)
                line = (
                    f"• {ob.title} — оплата за {period} отмечена "
                    f"(срок {fmt_date(occurrence.nominal_due, reference)})"
                )
                if following is not None and occurrence is paid[-1]:
                    line += f"; следующий срок {fmt_date(following.effective_due, reference)}"
                marked_rows.append(line + ".")
    heading = (
        "Сейчас оплачено"
        if bounds is None
        else f"Оплаченные сроки на период с {bounds[0]:%d.%m.%Y} по {bounds[1]:%d.%m.%Y}"
    )
    blocks = []
    if coverage_rows:
        blocks.append(heading + ":\n" + "\n".join(coverage_rows))
    if marked_rows:
        blocks.append(CALENDAR_MARKS + ":\n" + "\n".join(marked_rows) + "\n" + CALENDAR_NOTE)
    return "\n\n".join(blocks) if blocks else heading + ": подходящих записей нет."
