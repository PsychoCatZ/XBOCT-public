"""Turns due notification jobs into outbox messages (one short transaction per tick).

Order of checks for every due job:
1. still part of the current plan? (completion, payment, extension, cancellation or a
   schedule change make it stale -> cancelled, never sent);
2. quiet hours -> automatic jobs wait for their end (explicit user reminders do not);
3. missed for more than 6 hours (process was down) -> one catch-up message (within the
   daily budget), or carried by the weekly digest created in the same tick; if that
   digest is not delivered, the jobs return to pending: no avalanche, nothing lost;
4. subject covered by a CONFIRMED digest today -> ordinary duplicate suppressed;
5. at most one automatic message per subject per local day, and at most
   `max_auto_messages_per_day` automatic messages per day; the excess waits for the next
   day (nothing is dropped); several jobs in one tick are merged into one message.
Explicit user reminders (a named time, exact due time) bypass 2–5; a «позже» snooze
respects quiet hours but bypasses 3–5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xboct.application.planning import still_wanted
from xboct.application.render import fmt_instant
from xboct.application.uow import Uow
from xboct.domain.timeutil import in_quiet_hours, local_to_utc, to_local
from xboct.scheduling.outbox import enqueue
from xboct.scheduling.texts import render_job, render_many
from xboct.storage.models import DigestCoverage, NotificationJob, OutboxMessage

MISSED_AFTER = timedelta(hours=6)
ACTIVE_OUTBOX = ("pending", "sending", "sent", "unknown")


@dataclass
class DispatchStats:
    stale: int = 0
    deferred: int = 0
    suppressed: int = 0
    messages: list[str] = field(default_factory=list)


def quiet_end_after(uow: Uow, now: datetime) -> datetime | None:
    s = uow.settings
    local = to_local(now, uow.tz)
    if not in_quiet_hours(local.time(), s.quiet_start, s.quiet_end):
        return None
    day = local.date()
    if s.quiet_start > s.quiet_end and local.time() >= s.quiet_start:
        day += timedelta(days=1)
    return local_to_utc(day, s.quiet_end, uow.tz)


def next_day_start(uow: Uow, today: date) -> datetime:
    return local_to_utc(today + timedelta(days=1), uow.settings.quiet_end, uow.tz)


async def auto_count_today(session: AsyncSession, today: date) -> int:
    return (
        await session.scalar(
            select(func.count(OutboxMessage.id)).where(
                OutboxMessage.local_date == today,
                OutboxMessage.auto,
                OutboxMessage.status.in_(ACTIVE_OUTBOX),
            )
        )
        or 0
    )


async def subjects_messaged_today(session: AsyncSession, today: date) -> set[tuple[str, str]]:
    rows = await session.scalars(
        select(OutboxMessage).where(
            OutboxMessage.local_date == today,
            OutboxMessage.auto,
            OutboxMessage.kind == "notification",
            OutboxMessage.status.in_(ACTIVE_OUTBOX),
        )
    )
    return {(s[0], s[1]) for m in rows for s in m.subjects}


async def covered_by_digest(session: AsyncSession, today: date) -> set[tuple[str, str]]:
    rows = await session.execute(
        select(DigestCoverage.subject_type, DigestCoverage.subject_id).where(
            DigestCoverage.local_date == today
        )
    )
    return {(r[0], r[1]) for r in rows}


def _defer(job: NotificationJob, until: datetime, reason: str) -> None:
    job.scheduled_at = until
    job.defer_reason = reason
    job.deferred_count += 1


def _subject(job: NotificationJob) -> tuple[str, str]:
    return (job.subject_type, job.subject_id)


async def dispatch_due(
    uow: Uow, chat_id: int, *, digest_outbox_id: str | None = None
) -> DispatchStats:
    session, now = uow.session, uow.now
    today = uow.today_now
    stats = DispatchStats()
    due = list(
        await session.scalars(
            select(NotificationJob)
            .where(NotificationJob.status == "pending", NotificationJob.scheduled_at <= now)
            .order_by(NotificationJob.scheduled_at, NotificationJob.id)
        )
    )
    live: list[NotificationJob] = []
    for job in due:
        if await still_wanted(uow, job):
            live.append(job)
        else:
            job.status = "cancelled"
            job.cancelled_at = now
            job.cancel_reason = "stale"
            stats.stale += 1

    quiet_until = quiet_end_after(uow, now)
    if quiet_until is not None:
        for job in [j for j in live if not j.explicit]:
            _defer(job, quiet_until, "quiet_hours")
            stats.deferred += 1
        live = [j for j in live if j.explicit]
    # Asked for by the owner (a named time or «позже»): own message, outside the limits.
    requested = [j for j in live if j.explicit or j.kind == "snooze"]
    auto = [j for j in live if j not in requested]

    for job in requested:
        rendered = await render_job(uow, job)
        message = await enqueue(
            session,
            kind="notification",
            chat_id=chat_id,
            text=rendered.text,
            buttons=rendered.buttons,
            dedup_key=f"job:{job.id}",
            now=now,
            local_day=today,
            auto=False,
            subjects=[_subject(job)],
        )
        _mark_dispatched(job, message.id)
        stats.messages.append(message.id)

    # Lateness counts from the plan, so a quiet-hours hold does not hide a missed reminder.
    # Jobs held back by our own daily limits are not "missed".
    missed = [
        j
        for j in auto
        if now - j.planned_at > MISSED_AFTER
        and j.defer_reason not in ("daily_limit", "subject_daily")
    ]
    missed_subjects = {_subject(j) for j in missed}
    missed += [j for j in auto if j not in missed and _subject(j) in missed_subjects]
    current = [j for j in auto if j not in missed]

    digest = await session.get(OutboxMessage, digest_outbox_id) if digest_outbox_id else None
    if missed:
        if digest is not None and digest.status in ("pending", "sending", "sent"):
            # The weekly digest of this tick shows the current state. Delivered -> the
            # reminders are done; still in flight -> they ride on it and are settled with
            # it (failed or final unknown -> back to pending, see OutboxSender._finish_jobs).
            for job in missed:
                _mark_dispatched(job, digest.id)
                if digest.status == "sent":
                    job.status = "sent"
        elif await auto_count_today(session, today) >= uow.settings.max_auto_messages_per_day:
            # The catch-up is an automatic message too: it waits for tomorrow's budget.
            for job in missed:
                _defer(job, next_day_start(uow, today), "daily_limit")
                stats.deferred += 1
        else:
            header = (
                "Пока я был недоступен, подошли сроки (напоминания задержались, "
                f"сейчас {fmt_instant(now, uow.tz, today)}):"
            )
            rendered = await render_many(uow, _one_per_subject(missed), header)
            message = await enqueue(
                session,
                kind="catch_up",
                chat_id=chat_id,
                text=rendered.text,
                buttons=rendered.buttons,
                dedup_key=f"catch_up:{min(j.id for j in missed)}",
                now=now,
                local_day=today,
                auto=True,
                subjects=sorted({_subject(j) for j in missed}),
            )
            for job in missed:
                _mark_dispatched(job, message.id)
            stats.messages.append(message.id)

    covered = await covered_by_digest(session, today)
    already = await subjects_messaged_today(session, today)
    sendable: list[NotificationJob] = []
    for job in current:
        if _subject(job) in covered:
            job.status = "suppressed"
            job.cancel_reason = "digest_covered"
            job.cancelled_at = now
            stats.suppressed += 1
        elif _subject(job) in already:
            _defer(job, next_day_start(uow, today), "subject_daily")
            stats.deferred += 1
        else:
            sendable.append(job)

    if sendable:
        if await auto_count_today(session, today) >= uow.settings.max_auto_messages_per_day:
            for job in sendable:
                _defer(job, next_day_start(uow, today), "daily_limit")
                stats.deferred += 1
        else:
            first = _one_per_subject(sendable)
            if len(first) == 1:
                rendered = await render_job(uow, first[0])
            else:
                rendered = await render_many(uow, first, "Напоминания:")
            message = await enqueue(
                session,
                kind="notification",
                chat_id=chat_id,
                text=rendered.text,
                buttons=rendered.buttons,
                dedup_key=f"job:{min(j.id for j in first)}",
                now=now,
                local_day=today,
                auto=True,
                subjects=sorted({_subject(j) for j in first}),
            )
            for job in sendable:  # earlier stages of the same period ride along
                _mark_dispatched(job, message.id)
            stats.messages.append(message.id)
    await session.flush()
    return stats


def _one_per_subject(jobs: list[NotificationJob]) -> list[NotificationJob]:
    """Latest stage per subject and period (overdue over due over advance)."""
    order = {"advance": 0, "due": 1, "snooze": 1, "balance_low": 1, "overdue": 2}
    best: dict[tuple[str, str, str | None], NotificationJob] = {}
    for job in jobs:
        key = (*_subject(job), job.occurrence_id)
        if key not in best or order[job.kind] > order[best[key].kind]:
            best[key] = job
    return list(best.values())


def _mark_dispatched(job: NotificationJob, outbox_id: str) -> None:
    job.status = "dispatched"
    job.outbox_id = outbox_id


async def verify_outbox(uow: Uow, message: OutboxMessage) -> bool:
    """Right before each attempt: every reminder in the message must still be current."""
    if message.kind not in ("notification", "catch_up"):
        return True
    jobs = list(
        await uow.session.scalars(
            select(NotificationJob).where(
                NotificationJob.outbox_id == message.id, NotificationJob.status == "dispatched"
            )
        )
    )
    stale = [j for j in jobs if not await still_wanted(uow, j)]
    if not stale:
        return bool(jobs)
    for job in jobs:
        if job in stale:
            job.status = "cancelled"
            job.cancel_reason = "stale"
            job.cancelled_at = uow.now
        else:  # still current: plan it again on its own
            job.status = "pending"
            job.outbox_id = None
            job.scheduled_at = uow.now
    return False
