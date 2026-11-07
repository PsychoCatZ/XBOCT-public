"""Outbox: durable queue of outgoing messages with lease, retries and unknown outcomes.

Policy (docs/NOTIFICATIONS.md):
- retry_after -> wait as told; transient -> 1m / 5m / 30m, then failed;
- permanent (forbidden, bad request) -> failed, visible in diagnostics;
- unknown (Telegram may have accepted it) -> no immediate resend: at least 30 minutes,
  at most 2 attempts in total, then stays `unknown`; a possible duplicate is logged;
- a message left in `sending` by a crash is treated as unknown on restart;
- automatic messages wait for the end of quiet hours; replies never wait;
- reminders are re-verified right before every attempt and dropped when stale.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xboct.storage.models import (
    Delivery,
    DigestCoverage,
    NotificationJob,
    OutboxMessage,
)
from xboct.telegram.transport import Button, Transport, TransportError

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=2)
TRANSIENT_BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
UNKNOWN_RETRY_DELAY = timedelta(minutes=30)
UNKNOWN_MAX_ATTEMPTS = 2
REDACTED_TEXT = "[сообщение с кодом входа скрыто после отправки]"

# (session, message, now) -> True when the message may still go out
Verifier = Callable[[AsyncSession, OutboxMessage, datetime], Awaitable[bool]]
# (session, now) -> instant when automatic sending is allowed again, or None if allowed now
QuietGate = Callable[[AsyncSession, datetime], Awaitable[datetime | None]]


def buttons_to_json(buttons: list[list[Button]]) -> list[Any]:
    return [[{"text": b.text, "data": b.data} for b in row] for row in buttons]


def buttons_from_json(data: list[Any]) -> list[list[Button]]:
    return [[Button(b["text"], b["data"]) for b in row] for row in data or []]


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    chat_id: int,
    text: str,
    dedup_key: str,
    now: datetime,
    local_day: date,
    buttons: list[list[Button]] | None = None,
    auto: bool = False,
    subjects: list[tuple[str, str]] | None = None,
    sensitive: bool = False,
) -> OutboxMessage:
    """Idempotent: the same dedup_key returns the existing message."""
    existing = await session.scalar(
        select(OutboxMessage).where(OutboxMessage.dedup_key == dedup_key)
    )
    if existing is not None:
        return existing
    subjects = subjects or []
    message = OutboxMessage(
        kind=kind,
        chat_id=chat_id,
        text=text[:4096],
        buttons=buttons_to_json(buttons or []),
        dedup_key=dedup_key,
        subject_type=subjects[0][0] if len(subjects) == 1 else None,
        subject_id=subjects[0][1] if len(subjects) == 1 else None,
        subjects=[list(s) for s in subjects],
        auto=auto,
        local_date=local_day,
        status="pending",
        attempts=0,
        next_attempt_at=now,
        created_at=now,
        sensitive=sensitive,
    )
    session.add(message)
    await session.flush()
    return message


class OutboxSender:
    def __init__(
        self,
        sessions: Callable[[], AsyncSession],
        transport: Transport,
        now: Callable[[], datetime],
        *,
        verify: Verifier,
        quiet_gate: QuietGate,
    ) -> None:
        self.sessions = sessions
        self.transport = transport
        self.now = now
        self.verify = verify
        self.quiet_gate = quiet_gate

    async def reclaim(self, *, all_sending: bool = False) -> int:
        """A `sending` message may or may not have reached Telegram: treat it as unknown.

        `all_sending=True` is used right after the exclusive process lock is taken: no
        other process can be sending, so every leftover `sending` row belongs to a dead
        process, even if its lease has not expired yet (fast restart). Otherwise only
        expired leases are reclaimed (periodic safety net).
        """
        now = self.now()
        async with self.sessions() as session, session.begin():
            query = select(OutboxMessage).where(OutboxMessage.status == "sending")
            if not all_sending:
                query = query.where(OutboxMessage.lease_until < now)
            rows = list(await session.scalars(query))
            for message in rows:
                attempt = await session.scalar(
                    select(Delivery)
                    .where(Delivery.outbox_id == message.id, Delivery.status == "sending")
                    .order_by(Delivery.id.desc())
                    .limit(1)
                )
                if attempt is not None:
                    attempt.status = "unknown"
                    attempt.error = "interrupted while sending (process stopped)"
                    attempt.finished_at = now
                await self._after_unknown(session, message, now, "interrupted while sending")
        return len(rows)

    async def send_due(self, limit: int = 50, stop: asyncio.Event | None = None) -> int:
        """Send what is due. Once `stop` is set no further message is claimed: the one in
        flight finishes, the rest stays pending for the next start."""
        sent = 0
        for _ in range(limit):
            if stop is not None and stop.is_set():
                break
            claimed = await self._claim(stop)
            if claimed is None:
                break
            message_id, delivery_id, chat_id, text, buttons = claimed
            try:
                provider_id = await self.transport.send_message(chat_id, text, buttons)
            except TransportError as exc:
                await self._record_failure(message_id, delivery_id, exc)
                continue
            await self._record_success(message_id, delivery_id, provider_id)
            sent += 1
        return sent

    async def _claim(
        self, stop: asyncio.Event | None = None
    ) -> tuple[str, int, int, str, list[list[Button]]] | None:
        now = self.now()
        async with self.sessions() as session, session.begin():
            # Checked inside the write transaction, not only before it: whatever this
            # claim can see was committed before it began, so if that includes something
            # queued after the stop request, the stop is visible here too. A claim that
            # started earlier finds nothing newer and its send is the one in flight.
            # The transaction begins lazily (BEGIN IMMEDIATE at the first use of the
            # connection), so it is started explicitly before the check.
            await session.connection()
            if stop is not None and stop.is_set():
                return None
            rows = list(
                await session.scalars(
                    select(OutboxMessage)
                    .where(OutboxMessage.status == "pending", OutboxMessage.next_attempt_at <= now)
                    .order_by(OutboxMessage.next_attempt_at, OutboxMessage.created_at)
                    .limit(10)
                )
            )
            for message in rows:
                if message.auto:
                    resume = await self.quiet_gate(session, now)
                    if resume is not None:
                        message.next_attempt_at = resume
                        continue
                if not await self.verify(session, message, now):
                    message.status = "cancelled"
                    message.last_error = "stale"
                    continue
                message.status = "sending"
                message.attempts += 1
                message.lease_until = now + LEASE
                delivery = Delivery(
                    outbox_id=message.id,
                    attempt=message.attempts,
                    status="sending",
                    started_at=now,
                )
                session.add(delivery)
                await session.flush()
                return (
                    message.id,
                    delivery.id,
                    message.chat_id,
                    message.text,
                    buttons_from_json(message.buttons),
                )
        return None

    async def _record_success(self, message_id: str, delivery_id: int, provider_id: int) -> None:
        now = self.now()
        async with self.sessions() as session, session.begin():
            message = await session.get(OutboxMessage, message_id)
            delivery = await session.get(Delivery, delivery_id)
            assert message is not None
            assert delivery is not None
            delivery.status = "sent"
            delivery.provider_message_id = provider_id
            delivery.finished_at = now
            message.status = "sent"
            message.sent_at = now
            message.provider_message_id = provider_id
            message.lease_until = None
            await self._finish_jobs(session, message, now, delivered=True)
            if message.kind == "digest":
                for subject_type, subject_id in message.subjects:
                    exists = await session.scalar(
                        select(DigestCoverage.id).where(
                            DigestCoverage.local_date == message.local_date,
                            DigestCoverage.subject_type == subject_type,
                            DigestCoverage.subject_id == subject_id,
                        )
                    )
                    if exists is None:
                        session.add(
                            DigestCoverage(
                                local_date=message.local_date,
                                subject_type=subject_type,
                                subject_id=subject_id,
                                outbox_id=message.id,
                            )
                        )

    async def _record_failure(self, message_id: str, delivery_id: int, exc: TransportError) -> None:
        now = self.now()
        async with self.sessions() as session, session.begin():
            message = await session.get(OutboxMessage, message_id)
            delivery = await session.get(Delivery, delivery_id)
            assert message is not None
            assert delivery is not None
            delivery.finished_at = now
            delivery.error = exc.detail or exc.kind
            message.lease_until = None
            message.last_error = f"{exc.kind}: {exc.detail}"[:200]
            if exc.kind == "retry_after":
                delivery.status = "retry_after"
                message.status = "pending"
                message.next_attempt_at = now + timedelta(seconds=max(exc.retry_after, 1))
                message.attempts -= 1  # flood control is not a failed attempt
            elif exc.kind == "transient":
                delivery.status = "transient"
                if message.attempts > len(TRANSIENT_BACKOFF):
                    message.status = "failed"
                    await self._finish_jobs(session, message, now, delivered=False)
                else:
                    message.status = "pending"
                    message.next_attempt_at = now + TRANSIENT_BACKOFF[message.attempts - 1]
            elif exc.kind == "permanent":
                delivery.status = "failed"
                message.status = "failed"
                await self._finish_jobs(session, message, now, delivered=False)
            else:
                delivery.status = "unknown"
                await self._after_unknown(session, message, now, exc.detail)

    async def _after_unknown(
        self, session: AsyncSession, message: OutboxMessage, now: datetime, detail: str
    ) -> None:
        message.lease_until = None
        message.last_error = f"unknown: {detail}"[:200]
        if message.attempts >= UNKNOWN_MAX_ATTEMPTS:
            message.status = "unknown"  # final: may have been delivered; not resent again
            await self._finish_jobs(session, message, now, delivered=False)
        else:
            message.status = "pending"
            message.next_attempt_at = now + UNKNOWN_RETRY_DELAY
        log.warning(
            "telegram send outcome unknown; a later retry may duplicate the message",
            extra={"outbox_id": message.id, "attempts": message.attempts},
        )

    @staticmethod
    async def _finish_jobs(
        session: AsyncSession, message: OutboxMessage, now: datetime, *, delivered: bool
    ) -> None:
        """Settle the reminders carried by a message whose delivery is over.

        Delivered -> sent. Not delivered: a reminder's own message -> failed (Telegram
        refused it). Reminders that only rode along on a weekly digest -> back to pending,
        so a regular catch-up still covers them (failed or final-unknown digest).
        A secret in the text (login code) is removed once delivery is settled.
        """
        if message.sensitive:
            message.text = REDACTED_TEXT
        jobs = await session.scalars(
            select(NotificationJob).where(
                NotificationJob.outbox_id == message.id, NotificationJob.status == "dispatched"
            )
        )
        for job in jobs:
            if delivered:
                job.status = "sent"
            elif message.kind == "digest":
                job.status = "pending"
                job.outbox_id = None
                job.scheduled_at = now
                job.defer_reason = "digest_undelivered"
            else:
                job.status = "failed"
