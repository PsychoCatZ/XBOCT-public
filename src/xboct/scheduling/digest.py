"""Weekly digest (default Monday 10:00 local), built from the current stored state.

- at most one per local week (digest_run.week_start is the primary key);
- after downtime it goes out once, marked as delayed; never two for one week;
- weeks whose digest time precedes the very first start are not owed;
- waits for the end of quiet hours;
- «напоминание отправлялось» comes only from confirmed deliveries, «последняя оплата»
  only from payments; nothing is inferred;
- subjects listed in a CONFIRMED digest suppress ordinary same-day duplicates
  (recorded by the outbox on successful delivery, never for pending/failed digests).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from xboct.application.queries import Agenda, AgendaItem, agenda, balance_forecasts, payments
from xboct.application.render import coverage_text, fmt_date, forecast_text
from xboct.application.uow import Uow
from xboct.domain.timeutil import local_to_utc, monday_of
from xboct.scheduling.notifier import MISSED_AFTER, quiet_end_after
from xboct.scheduling.outbox import enqueue
from xboct.scheduling.state import DIGEST_SINCE, get_state
from xboct.storage.models import DigestRun, OutboxMessage, Task

LONG_PENDING_DAYS = 14


def subject_of(item: AgendaItem) -> tuple[str, str]:
    return ("task" if item.kind in ("task", "task_occurrence") else "obligation", item.subject_id)


def week_and_due(uow: Uow) -> tuple[date, datetime]:
    today = uow.today_now
    week_start = monday_of(today)
    digest_day = week_start + timedelta(days=uow.settings.digest_weekday)
    return week_start, local_to_utc(digest_day, uow.settings.digest_time, uow.tz)


async def last_reminded(uow: Uow) -> dict[tuple[str, str], datetime]:
    rows = await uow.session.scalars(
        select(OutboxMessage).where(
            OutboxMessage.status == "sent",
            OutboxMessage.kind.in_(("notification", "catch_up")),
        )
    )
    seen: dict[tuple[str, str], datetime] = {}
    for message in rows:
        for subject_type, subject_id in message.subjects:
            key = (subject_type, subject_id)
            if message.sent_at and (key not in seen or message.sent_at > seen[key]):
                seen[key] = message.sent_at
    return seen


async def build_digest(uow: Uow, *, delayed: bool) -> tuple[str, list[tuple[str, str]]]:
    today = uow.today_now
    view: Agenda = await agenda(uow)
    reminded = await last_reminded(uow)
    covered: list[tuple[str, str]] = []
    blocks: list[str] = []
    week_end = today + timedelta(days=6)
    title = f"Сводка на неделю {fmt_date(today, today)} — {fmt_date(week_end, today)}"
    if delayed:
        title += " (с задержкой: в назначенное время я был недоступен)"

    def line(item: AgendaItem) -> str:
        text = f"• {item.text(uow)}"
        sent = reminded.get(subject_of(item))
        if sent is not None:
            text += f" (напоминание отправлялось {fmt_date(sent.astimezone(uow.tz).date(), today)})"
        return text

    if view.overdue:
        blocks.append("Срок прошёл, отметки нет:\n" + "\n".join(line(i) for i in view.overdue))
    upcoming = view.due_today + view.next_7_days
    if upcoming:
        blocks.append("На этой неделе:\n" + "\n".join(line(i) for i in upcoming))
    covered += [subject_of(i) for i in view.overdue + upcoming]

    low = [(ob, fc) for ob, fc in await balance_forecasts(uow) if fc.status in ("low", "depleted")]
    if low:
        blocks.append(
            "Баланс:\n"
            + "\n".join(
                f"• «{ob.title}»: {forecast_text(fc, ob.currency, uow.tz, today)}" for ob, fc in low
            )
        )
        covered += [("obligation", ob.id) for ob, _ in low]

    old_undated = list(
        await uow.session.scalars(
            select(Task)
            .where(
                Task.status == "open",
                Task.due_date.is_(None),
                Task.due_at.is_(None),
                Task.recurrence_id.is_(None),
                Task.created_at <= uow.now - timedelta(days=LONG_PENDING_DAYS),
            )
            .order_by(Task.created_at)
        )
    )
    if old_undated:
        blocks.append(
            "Давно без движения:\n"
            + "\n".join(
                f"• «{t.title}» — без срока, записано "
                f"{fmt_date(t.created_at.astimezone(uow.tz).date(), today)}"
                for t in old_undated
            )
        )

    attention = {sid for _, sid in covered}
    calm = []
    for view_row in await payments(uow):
        if view_row.obligation_id in attention:
            continue
        if view_row.kind == "calendar" and view_row.next_due and not view_row.outstanding:
            calm.append(f"«{view_row.title}» — следующий срок {fmt_date(view_row.next_due, today)}")
        elif view_row.kind == "coverage" and view_row.coverage_until:
            calm.append(f"«{view_row.title}» — {coverage_text(view_row.coverage_until, today)}")
        elif view_row.kind == "balance" and view_row.forecast and view_row.forecast.days_left_floor:
            calm.append(f"«{view_row.title}» — примерно {view_row.forecast.days_left_floor} дн.")
    if calm:
        blocks.append("Спокойно: " + "; ".join(calm) + ".")

    if not (view.overdue or upcoming or low or old_undated):
        blocks.insert(0, "На неделе спокойно.")
    return title + "\n\n" + "\n\n".join(blocks), sorted(set(covered))


async def maybe_create_digest(uow: Uow, chat_id: int) -> str | None:
    """Create this week's digest if it is due and was not created yet. Returns outbox id."""
    week_start, due_at = week_and_due(uow)
    if uow.now < due_at:
        return None
    since = await get_state(uow.session, DIGEST_SINCE)
    if since is None or due_at < datetime.fromisoformat(since["at"]):
        return None  # the service did not exist yet at that digest time: nothing is owed
    if await uow.session.get(DigestRun, week_start) is not None:
        return None
    if quiet_end_after(uow, uow.now) is not None:
        return None
    delayed = uow.now - due_at > MISSED_AFTER
    text, covered = await build_digest(uow, delayed=delayed)
    message = await enqueue(
        uow.session,
        kind="digest",
        chat_id=chat_id,
        text=text,
        dedup_key=f"digest:{week_start.isoformat()}",
        now=uow.now,
        local_day=uow.today_now,
        auto=True,
        subjects=covered,
    )
    uow.session.add(
        DigestRun(
            week_start=week_start,
            timezone=uow.settings.timezone,
            outbox_id=message.id,
            delayed=delayed,
            created_at=uow.now,
        )
    )
    await uow.session.flush()
    return message.id
