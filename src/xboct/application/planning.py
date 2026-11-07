"""Notification planning: deterministic job records, diffed against what is stored.

The scheduler/sender (xboct.scheduling) delivers. Rules:
- every job has dedup_key subject:period:kind:revision; a replan that changes anything
  bumps the subject's schedule_revision, cancels outdated pending jobs, adds new ones;
- a pending job whose (kind, period, instant) is still wanted is kept;
- past instants are not created anew (except balance_low, which reports a current state);
- a stage already sent (or deliberately snoozed away) is not planned again;
- automatic jobs leave quiet hours; due dates themselves never move.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select

from xboct.application.balance_ledger import load_forecast
from xboct.application.uow import Uow
from xboct.domain.timeutil import local_to_utc, shift_out_of_quiet, start_of_local_day
from xboct.storage.models import NotificationJob, Obligation, Occurrence, Recurrence, Task

JOB_HORIZON_DAYS = 62
# Balance below the threshold already at funding: one last warning this long before zero.
FINAL_WARNING_BEFORE = timedelta(days=1)
FINAL_SUFFIX = ":final"


@dataclass(frozen=True)
class DesiredJob:
    kind: str
    period_key: str
    scheduled_at: datetime
    occurrence_id: str | None = None
    explicit: bool = False

    @property
    def identity(self) -> tuple[str, str, datetime]:
        return (self.kind, self.period_key, self.scheduled_at)


def _identity(job: NotificationJob) -> tuple[str, str, datetime]:
    return (job.kind, job.period_key, job.planned_at)


# A stage in one of these states was handled already and is never planned again.
HANDLED_STATUSES = ("dispatched", "sent", "failed", "suppressed")


def _handled(job: NotificationJob) -> bool:
    return job.status in HANDLED_STATUSES or (
        job.status == "cancelled" and job.cancel_reason == "snoozed"
    )


class Planner:
    def __init__(self, uow: Uow) -> None:
        self.uow = uow
        s = uow.settings
        self.tz = uow.tz
        self.notice = s.default_notice_time
        self.quiet = (s.quiet_start, s.quiet_end)
        self.advance_days = s.payment_advance_days
        self.overdue_after = timedelta(hours=s.overdue_followup_hours)

    def quiet_shift(self, instant: datetime) -> datetime:
        return shift_out_of_quiet(instant, self.tz, *self.quiet)

    def at_notice(self, day: date) -> datetime:
        return self.quiet_shift(local_to_utc(day, self.notice, self.tz))

    def overdue_for_date(self, day: date) -> datetime:
        end_of_due_day = start_of_local_day(day + timedelta(days=1), self.tz)
        return self.quiet_shift(end_of_due_day + self.overdue_after)

    def due_jobs(
        self,
        period_key: str,
        due_date: date,
        due_at: datetime | None,
        occurrence_id: str | None,
        *,
        advance: bool,
    ) -> list[DesiredJob]:
        jobs: list[DesiredJob] = []
        if advance and self.advance_days > 0:
            jobs.append(
                DesiredJob(
                    "advance",
                    period_key,
                    self.at_notice(due_date - timedelta(days=self.advance_days)),
                    occurrence_id,
                )
            )
        if due_at is not None:
            jobs.append(DesiredJob("due", period_key, due_at, occurrence_id, explicit=True))
            overdue = self.quiet_shift(due_at + self.overdue_after)
        else:
            jobs.append(DesiredJob("due", period_key, self.at_notice(due_date), occurrence_id))
            overdue = self.overdue_for_date(due_date)
        jobs.append(DesiredJob("overdue", period_key, overdue, occurrence_id))
        return jobs

    def coverage_jobs(self, coverage_until: date) -> list[DesiredJob]:
        """coverage_until is the first unpaid day (exclusive boundary).

        advance: N days before the boundary; due: the last paid day ("сегодня последний
        оплаченный день"); overdue: one follow-up counted from the boundary itself
        (start of coverage_until + interval), never from the end of the first unpaid day.
        """
        key = f"until:{coverage_until.isoformat()}"
        last_paid = coverage_until - timedelta(days=1)
        jobs: list[DesiredJob] = []
        if self.advance_days > 1:
            jobs.append(
                DesiredJob(
                    "advance",
                    key,
                    self.at_notice(coverage_until - timedelta(days=self.advance_days)),
                )
            )
        jobs.append(DesiredJob("due", key, self.at_notice(last_paid)))
        boundary = start_of_local_day(coverage_until, self.tz)
        jobs.append(DesiredJob("overdue", key, self.quiet_shift(boundary + self.overdue_after)))
        return jobs

    async def occurrence_jobs(
        self, owner_type: str, owner_id: str, recurrence: Recurrence | None, *, advance: bool
    ) -> list[DesiredJob]:
        limit = self.uow.today_now + timedelta(days=JOB_HORIZON_DAYS)
        rows = await self.uow.session.scalars(
            select(Occurrence).where(
                Occurrence.owner_type == owner_type,
                Occurrence.owner_id == owner_id,
                Occurrence.status == "open",
                Occurrence.effective_due <= limit,
            )
        )
        jobs: list[DesiredJob] = []
        due_time = recurrence.due_time if recurrence is not None else None
        for occ in rows:
            due_at = local_to_utc(occ.effective_due, due_time, self.tz) if due_time else None
            jobs += self.due_jobs(
                occ.period_key, occ.effective_due, due_at, occ.id, advance=advance
            )
        return jobs

    async def desired_for_task(self, task: Task) -> list[DesiredJob]:
        if task.status != "open":
            return []
        if task.recurrence_id is not None:
            recurrence = await self.uow.session.get(Recurrence, task.recurrence_id)
            return await self.occurrence_jobs("task", task.id, recurrence, advance=False)
        if task.due_at is not None:
            due_date = task.due_at.astimezone(self.tz).date()
            return self.due_jobs("task", due_date, task.due_at, None, advance=False)
        if task.due_date is not None:
            return self.due_jobs("task", task.due_date, None, None, advance=False)
        return []

    async def desired_for_obligation(self, ob: Obligation) -> list[DesiredJob]:
        if not ob.active:
            return []
        if ob.kind == "calendar":
            recurrence = (
                await self.uow.session.get(Recurrence, ob.recurrence_id)
                if ob.recurrence_id
                else None
            )
            return await self.occurrence_jobs("obligation", ob.id, recurrence, advance=True)
        if ob.kind == "coverage":
            if ob.coverage_until is None:
                return []
            return self.coverage_jobs(ob.coverage_until)
        forecast = await load_forecast(self.uow, ob)
        if (
            forecast.threshold_at is None
            or forecast.depletion_at is None
            or forecast.funding_at is None
        ):
            return []
        key = f"funding:{forecast.funding_event_id}"
        if forecast.threshold_at > forecast.funding_at:
            return [DesiredJob("balance_low", key, self.quiet_shift(forecast.threshold_at))]
        # Already below the threshold at the topup/correction: the reply carries that
        # warning (no immediate duplicate). With more than a day left, one final warning
        # a day before the estimated zero; with a day or less, only the reply says so.
        final_at = forecast.depletion_at - FINAL_WARNING_BEFORE
        if final_at <= forecast.funding_at:
            return []
        return [DesiredJob("balance_low", key + FINAL_SUFFIX, self.quiet_shift(final_at))]


async def _subject(uow: Uow, subject_type: str, subject_id: str) -> Task | Obligation | None:
    model = Task if subject_type == "task" else Obligation
    return await uow.session.get(model, subject_id)


async def replan_subject(uow: Uow, subject_type: str, subject_id: str) -> None:
    subject = await _subject(uow, subject_type, subject_id)
    if subject is None:
        return
    planner = Planner(uow)
    if isinstance(subject, Task):
        desired = await planner.desired_for_task(subject)
        active = subject.status == "open"
    else:
        desired = await planner.desired_for_obligation(subject)
        active = subject.active

    jobs = list(
        await uow.session.scalars(
            select(NotificationJob).where(
                NotificationJob.subject_type == subject_type,
                NotificationJob.subject_id == subject_id,
            )
        )
    )
    pending = [j for j in jobs if j.status == "pending" and j.kind != "snooze"]
    pending_snoozes = [j for j in jobs if j.status == "pending" and j.kind == "snooze"]
    done = {_identity(j) for j in jobs if _handled(j)}
    done_balance = {j.period_key for j in jobs if j.kind == "balance_low" and _handled(j)}
    wanted = {d.identity for d in desired}
    pending_ids = {_identity(j) for j in pending}

    to_cancel = [j for j in pending if _identity(j) not in wanted]
    open_occurrences = {d.occurrence_id for d in desired if d.occurrence_id}
    for snooze in pending_snoozes:
        stale = not active or (
            snooze.occurrence_id is not None
            and snooze.occurrence_id not in open_occurrences
            and not await _occurrence_open(uow, snooze.occurrence_id)
        )
        if stale:
            to_cancel.append(snooze)

    to_create: list[DesiredJob] = []
    for d in desired:
        if d.identity in pending_ids or d.identity in done:
            continue
        if d.kind == "balance_low" and d.period_key in done_balance:
            continue  # one warning of each kind per funding epoch
        # Only the threshold warning reports a current state and may be due already;
        # every other stage, the final balance warning included, is never created late.
        threshold = d.kind == "balance_low" and not d.period_key.endswith(FINAL_SUFFIX)
        if not threshold and d.scheduled_at < uow.now:
            continue
        to_create.append(d)

    if not to_cancel and not to_create:
        return
    subject.schedule_revision += 1
    revision = subject.schedule_revision
    reason = "closed" if not active else "replanned"
    for job in to_cancel:
        job.status = "cancelled"
        job.cancelled_at = uow.now
        job.cancel_reason = reason
    for d in to_create:
        uow.session.add(
            NotificationJob(
                subject_type=subject_type,
                subject_id=subject_id,
                occurrence_id=d.occurrence_id,
                period_key=d.period_key,
                kind=d.kind,
                planned_at=d.scheduled_at,
                scheduled_at=d.scheduled_at,
                revision=revision,
                explicit=d.explicit,
                status="pending",
                dedup_key=f"{subject_type}:{subject_id}:{d.period_key}:{d.kind}:{revision}",
                command_id=uow.audit_command_id,
                created_at=uow.now,
            )
        )
    await uow.session.flush()


async def still_wanted(uow: Uow, job: NotificationJob) -> bool:
    """Is this reminder still part of the current plan? Checked right before sending.

    Completion, payment, extension, cancellation, archive or any schedule change makes
    the stored plan differ from the recomputed one, so a stale reminder is not sent.
    """
    subject = await _subject(uow, job.subject_type, job.subject_id)
    if subject is None:
        return False
    active = subject.status == "open" if isinstance(subject, Task) else subject.active
    if not active:
        return False
    if job.occurrence_id is not None and not await _occurrence_open(uow, job.occurrence_id):
        return False
    if job.kind == "snooze":
        return True
    planner = Planner(uow)
    desired = (
        await planner.desired_for_task(subject)
        if isinstance(subject, Task)
        else await planner.desired_for_obligation(subject)
    )
    return _identity(job) in {d.identity for d in desired}


async def _occurrence_open(uow: Uow, occurrence_id: str) -> bool:
    occ = await uow.session.get(Occurrence, occurrence_id)
    return occ is not None and occ.status == "open"


async def snooze(
    uow: Uow,
    subject: Task | Obligation,
    subject_type: str,
    occurrence: Occurrence | None,
    until: datetime,
    *,
    explicit: bool,
) -> NotificationJob:
    """Replace the pending reminder(s) up to `until` with one snooze job. Due is untouched."""
    jobs = await uow.session.scalars(
        select(NotificationJob).where(
            NotificationJob.subject_type == subject_type,
            NotificationJob.subject_id == subject.id,
            NotificationJob.status == "pending",
        )
    )
    for job in jobs:
        if occurrence is not None and job.occurrence_id not in (None, occurrence.id):
            continue  # another period of the same subject keeps its reminders
        if job.kind == "snooze" or job.scheduled_at <= until:
            job.status = "cancelled"
            job.cancelled_at = uow.now
            job.cancel_reason = "snoozed"
    subject.schedule_revision += 1
    period_key = occurrence.period_key if occurrence is not None else "-"
    scheduled = (
        until
        if explicit
        else shift_out_of_quiet(until, uow.tz, uow.settings.quiet_start, uow.settings.quiet_end)
    )
    job = NotificationJob(
        subject_type=subject_type,
        subject_id=subject.id,
        occurrence_id=occurrence.id if occurrence is not None else None,
        period_key=period_key,
        kind="snooze",
        planned_at=scheduled,
        scheduled_at=scheduled,
        revision=subject.schedule_revision,
        explicit=explicit,
        status="pending",
        dedup_key=f"{subject_type}:{subject.id}:{period_key}:snooze:{subject.schedule_revision}",
        command_id=uow.audit_command_id,
        created_at=uow.now,
    )
    uow.session.add(job)
    await uow.session.flush()
    return job
