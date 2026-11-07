"""SQLite schema. Logical contract: docs/DATA_MODEL.md.

IDs are text UUIDs, instants are UTC, deadlines «до дня» are local dates,
money is integer minor units + currency. Versions are bumped explicitly by the
application layer when user-visible fields change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Time,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, returns aware UTC. Rejects naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be stored")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        date: Date(),
        time: Time(),
        dict[str, Any]: JSON(),
        list[Any]: JSON(),
    }


class AppSettings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64))
    quiet_start: Mapped[time]
    quiet_end: Mapped[time]
    digest_weekday: Mapped[int] = mapped_column(Integer)
    digest_time: Mapped[time]
    default_notice_time: Mapped[time]
    evening_time: Mapped[time]
    later_minutes: Mapped[int] = mapped_column(Integer)
    payment_advance_days: Mapped[int] = mapped_column(Integer)
    balance_threshold_days: Mapped[int] = mapped_column(Integer)
    overdue_followup_hours: Mapped[int] = mapped_column(Integer)
    daily_digest_enabled: Mapped[bool] = mapped_column(Boolean)
    max_auto_messages_per_day: Mapped[int] = mapped_column(Integer)
    profanity_enabled: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime]

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class Recurrence(Base):
    __tablename__ = "recurrence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(32))
    frequency: Mapped[str] = mapped_column(String(16))
    interval: Mapped[int] = mapped_column(Integer, default=1)
    weekdays: Mapped[list[Any]] = mapped_column(default=list)
    monthday: Mapped[int | None] = mapped_column(Integer)
    anchor_date: Mapped[date]
    due_time: Mapped[time | None]
    materialized_until: Mapped[date | None]
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("owner_type IN ('task', 'obligation')", name="owner_type"),
        CheckConstraint("frequency IN ('daily', 'weekly', 'monthly', 'yearly')", name="frequency"),
        CheckConstraint("interval >= 1", name="interval_positive"),
        UniqueConstraint("owner_type", "owner_id"),
    )


class Task(Base):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2000))
    next_action: Mapped[str | None] = mapped_column(String(300))
    due_date: Mapped[date | None]
    due_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default="open")
    recurrence_id: Mapped[str | None] = mapped_column(ForeignKey("recurrence.id"))
    schedule_revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("status IN ('open', 'completed', 'cancelled')", name="status"),
        Index(None, "status", "due_date"),
    )


class Obligation(Base):
    __tablename__ = "obligation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[Any]] = mapped_column(default=list)
    kind: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    coverage_until: Mapped[date | None]
    recurrence_id: Mapped[str | None] = mapped_column(ForeignKey("recurrence.id"))
    schedule_revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("kind IN ('calendar', 'coverage', 'balance')", name="kind"),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="amount_non_negative"),
    )


class Occurrence(Base):
    __tablename__ = "occurrence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(32))
    recurrence_id: Mapped[str] = mapped_column(ForeignKey("recurrence.id"))
    period_key: Mapped[str] = mapped_column(String(32))
    nominal_due: Mapped[date]
    effective_due: Mapped[date]
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolved_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "period_key"),
        CheckConstraint(
            "status IN ('open', 'done', 'paid', 'skipped', 'cancelled', 'superseded')",
            name="status",
        ),
        Index(None, "status", "effective_due"),
        Index(None, "owner_type", "owner_id", "nominal_due"),
    )


class Payment(Base):
    __tablename__ = "payment"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("obligation.id"))
    occurrence_id: Mapped[str | None] = mapped_column(ForeignKey("occurrence.id"))
    paid_at: Mapped[datetime]
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    coverage_from: Mapped[date | None]
    coverage_until: Mapped[date | None]
    command_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]
    reverted_at: Mapped[datetime | None]
    reverted_by: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        CheckConstraint("occurrence_id IS NOT NULL OR coverage_until IS NOT NULL", name="target"),
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="amount_non_negative"),
        Index(None, "obligation_id", "paid_at"),
    )


class BalanceEvent(Base):
    __tablename__ = "balance_event"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("obligation.id"))
    seq: Mapped[int] = mapped_column(Integer)
    effective_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(16))
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    rate_minor_per_day: Mapped[int | None] = mapped_column(Integer)
    assumed: Mapped[bool] = mapped_column(Boolean, default=False)
    command_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]
    reverted_at: Mapped[datetime | None]
    reverted_by: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        CheckConstraint("kind IN ('topup', 'correction', 'rate_change')", name="kind"),
        CheckConstraint(
            "rate_minor_per_day IS NULL OR rate_minor_per_day >= 0", name="rate_non_negative"
        ),
        CheckConstraint(
            "kind != 'topup' OR (amount_minor IS NOT NULL AND amount_minor > 0)",
            name="topup_positive",
        ),
        UniqueConstraint("obligation_id", "seq"),
        Index(None, "obligation_id", "effective_at"),
    )


class CommandReceipt(Base):
    __tablename__ = "command_receipt"

    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_key: Mapped[str | None] = mapped_column(String(128))
    command_type: Mapped[str] = mapped_column(String(48))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any]]
    received_at: Mapped[datetime]
    applied_at: Mapped[datetime]
    undoable: Mapped[bool] = mapped_column(Boolean, default=False)
    undone_by: Mapped[str | None] = mapped_column(String(128))


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(32))
    parent_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]

    __table_args__ = (Index(None, "entity_id"), Index(None, "parent_id"))


class PendingIntent(Base):
    __tablename__ = "pending_intent"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    command_id: Mapped[str] = mapped_column(String(128))
    actions: Mapped[list[Any]]
    candidates: Mapped[list[Any]]
    expected_versions: Mapped[dict[str, Any]]
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired', 'superseded')",
            name="status",
        ),
        Index(None, "status", "expires_at"),
    )


class NotificationJob(Base):
    __tablename__ = "notification_job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(32))
    occurrence_id: Mapped[str | None] = mapped_column(ForeignKey("occurrence.id"))
    period_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    # planned_at is the plan's identity (never moves); scheduled_at is when delivery is
    # attempted and may be deferred by quiet hours or daily limits.
    planned_at: Mapped[datetime]
    scheduled_at: Mapped[datetime]
    revision: Mapped[int] = mapped_column(Integer)
    explicit: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    command_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime]
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(String(32))
    outbox_id: Mapped[str | None] = mapped_column(String(32))
    defer_reason: Mapped[str | None] = mapped_column(String(32))
    deferred_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('advance', 'due', 'overdue', 'balance_low', 'snooze', 'digest')",
            name="kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'dispatched', 'sent', 'failed', 'suppressed', 'cancelled')",
            name="status",
        ),
        Index(None, "status", "scheduled_at"),
        Index(None, "subject_type", "subject_id", "status"),
    )


class OutboxMessage(Base):
    """Every outgoing Telegram message: replies, reminders, catch-up and digests."""

    __tablename__ = "outbox_message"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list[Any]] = mapped_column(default=list)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    subject_type: Mapped[str | None] = mapped_column(String(16))
    subject_id: Mapped[str | None] = mapped_column(String(32))
    subjects: Mapped[list[Any]] = mapped_column(default=list)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    local_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime]
    lease_until: Mapped[datetime | None]
    provider_message_id: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    # A secret (login code) is in the text: it is replaced once delivery is settled.
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        CheckConstraint("kind IN ('reply', 'notification', 'catch_up', 'digest')", name="kind"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'unknown', 'cancelled')",
            name="status",
        ),
        Index(None, "status", "next_attempt_at"),
        Index(None, "local_date", "auto"),
    )


class Delivery(Base):
    """One attempt to hand an outbox message to Telegram (history, never edited)."""

    __tablename__ = "delivery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outbox_id: Mapped[str] = mapped_column(ForeignKey("outbox_message.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    provider_message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'transient', 'retry_after', 'unknown')",
            name="status",
        ),
    )


class TelegramInbox(Base):
    """Telegram updates are stored before the offset is acknowledged."""

    __tablename__ = "telegram_inbox"

    update_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(String(16))
    from_id: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]]
    received_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), default="received")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    processed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processing', 'done', 'rejected', 'ignored', 'failed')",
            name="status",
        ),
        Index(None, "status", "update_id"),
    )


class RuntimeState(Base):
    """Small durable key/value facts: Telegram offset, last daily refresh, heartbeats."""

    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime]


class DigestRun(Base):
    """At most one weekly digest per local week."""

    __tablename__ = "digest_run"

    week_start: Mapped[date] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64))
    outbox_id: Mapped[str] = mapped_column(String(32))
    delayed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]


class DigestCoverage(Base):
    """Subjects a CONFIRMED digest covered; ordinary same-day duplicates are suppressed."""

    __tablename__ = "digest_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_date: Mapped[date]
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(32))
    outbox_id: Mapped[str] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("local_date", "subject_type", "subject_id"),)


class LlmCall(Base):
    """One proposal request to a language model: bookkeeping only, never prompt text."""

    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    local_date: Mapped[date] = mapped_column(index=True)
    command_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    error_kind: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("status IN ('ok', 'error')", name="status"),)


class AuthCode(Base):
    """One-time web login code, issued in the owner's private Telegram chat."""

    __tablename__ = "auth_code"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    failures: Mapped[int] = mapped_column(Integer, default=0)
    invalidated_at: Mapped[datetime | None]


class WebSession(Base):
    """Server-side session; the browser holds only a random token (hash stored here)."""

    __tablename__ = "web_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class LoginAttempt(Base):
    """Code submissions, for throttling. The IP is stored hashed."""

    __tablename__ = "login_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(index=True)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean)
