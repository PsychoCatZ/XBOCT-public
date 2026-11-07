"""Typed application commands. Web forms, Telegram buttons and resolved LLM proposals
all end up here. Dates are already absolute; relative expressions live in domain.dates.

For update commands, a field that is present (even as null) is applied; an absent
field is left unchanged (pydantic `model_fields_set`).
"""

from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from xboct.domain.coverage import CoverageUnit

ID = Annotated[str, Field(min_length=1, max_length=32)]
Title = Annotated[str, Field(min_length=1, max_length=200)]
Note = Annotated[str, Field(max_length=4000)]
Version = Annotated[int, Field(ge=1)]
Minor = Annotated[int, Field(ge=0, le=10**13)]

ALLOWED_URL_SCHEMES = ("http", "https")


def validate_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise ValueError("ссылка должна начинаться с http:// или https://")
    if len(value) > 2000:
        raise ValueError("слишком длинная ссылка")
    return value


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RecurrenceSpec(_Command):
    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    anchor_date: date
    interval: int = Field(default=1, ge=1, le=366)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=list)
    monthday: int | None = Field(default=None, ge=1, le=31)
    due_time: time | None = None


# --- tasks -----------------------------------------------------------------------------


class CreateTask(_Command):
    type: Literal["create_task"] = "create_task"
    title: Title
    note: Note | None = None
    url: str | None = None
    next_action: Annotated[str, Field(max_length=300)] | None = None
    due_date: date | None = None
    due_time: time | None = None
    recurrence: RecurrenceSpec | None = None

    _url = field_validator("url")(validate_url)


class UpdateTask(_Command):
    type: Literal["update_task"] = "update_task"
    task_id: ID
    expected_version: Version
    title: Title | None = None
    note: Note | None = None
    url: str | None = None
    next_action: Annotated[str, Field(max_length=300)] | None = None
    due_date: date | None = None
    due_time: time | None = None
    recurrence: RecurrenceSpec | None = None

    _url = field_validator("url")(validate_url)


class CompleteTask(_Command):
    type: Literal["complete_task"] = "complete_task"
    task_id: ID
    expected_version: Version
    occurrence_id: ID | None = None
    occurrence_version: Version | None = None


class CancelTask(_Command):
    type: Literal["cancel_task"] = "cancel_task"
    task_id: ID
    expected_version: Version


class RescheduleOccurrence(_Command):
    """Move one period's effective due; the nominal period and the rule are kept."""

    type: Literal["reschedule_occurrence"] = "reschedule_occurrence"
    occurrence_id: ID
    expected_version: Version
    new_due: date


class SkipOccurrence(_Command):
    type: Literal["skip_occurrence"] = "skip_occurrence"
    occurrence_id: ID
    expected_version: Version


# --- obligations ----------------------------------------------------------------------


class BalanceStart(_Command):
    """Balance needs a baseline: a known checkpoint or an explicit «считаем от нуля»."""

    checkpoint_minor: Minor | None = None
    checkpoint_at: AwareDatetime | None = None
    assume_zero: bool = False
    rate_minor_per_day: int | None = Field(default=None, ge=0)


class CreateObligation(_Command):
    type: Literal["create_obligation"] = "create_obligation"
    title: Title
    kind: Literal["calendar", "coverage", "balance"]
    aliases: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    amount_minor: Minor | None = None
    note: Note | None = None
    recurrence: RecurrenceSpec | None = None
    coverage_until: date | None = None
    balance: BalanceStart | None = None
    duplicates: Literal["unchecked", "forbid", "allow"] = "unchecked"
    """Text creation: «forbid» refuses a name that already exists (checked in the applying
    transaction, so a preview cannot create a twin), «allow» is the owner's explicit second
    record; forms leave it «unchecked»."""


class UpdateObligation(_Command):
    type: Literal["update_obligation"] = "update_obligation"
    obligation_id: ID
    expected_version: Version
    title: Title | None = None
    aliases: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = None
    amount_minor: Minor | None = None
    note: Note | None = None
    recurrence: RecurrenceSpec | None = None


class OccurrenceRef(_Command):
    id: ID
    expected_version: Version


class MarkPayment(_Command):
    """Calendar obligation: close the selected periods. Others stay as they are."""

    type: Literal["mark_payment"] = "mark_payment"
    obligation_id: ID
    occurrences: list[OccurrenceRef] = Field(min_length=1, max_length=60)
    amount_minor: Minor | None = None
    paid_at: AwareDatetime | None = None


class ExtendCoverage(_Command):
    """Coverage obligation: move coverage_until (exclusive) forward."""

    type: Literal["extend_coverage"] = "extend_coverage"
    obligation_id: ID
    expected_version: Version
    start: date | None = None
    unit: CoverageUnit | None = None
    count: int | None = Field(default=None, ge=1, le=120)
    until: date | None = None
    amount_minor: Minor | None = None
    paid_at: AwareDatetime | None = None


class RecordTopup(_Command):
    type: Literal["record_topup"] = "record_topup"
    obligation_id: ID
    amount_minor: Annotated[int, Field(gt=0, le=10**13)]
    effective_at: AwareDatetime | None = None
    start_from_zero: bool = False


class CorrectBalance(_Command):
    type: Literal["correct_balance"] = "correct_balance"
    obligation_id: ID
    amount_minor: Minor
    effective_at: AwareDatetime | None = None
    assumed: bool = False


class ChangeDailyRate(_Command):
    type: Literal["change_daily_rate"] = "change_daily_rate"
    obligation_id: ID
    rate_minor_per_day: int
    effective_at: AwareDatetime | None = None


class ArchiveObligation(_Command):
    type: Literal["archive_obligation"] = "archive_obligation"
    obligation_id: ID
    expected_version: Version
    outstanding: Literal["keep_open", "close"] | None = None


# --- notifications, settings, undo, pending -------------------------------------------


class SnoozeNotification(_Command):
    """New reminder instant. Never changes the due date."""

    type: Literal["snooze_notification"] = "snooze_notification"
    subject_type: Literal["task", "obligation"]
    subject_id: ID
    occurrence_id: ID | None = None
    until: AwareDatetime
    explicit: bool = True


class ChangeTimezone(_Command):
    type: Literal["change_timezone"] = "change_timezone"
    timezone: Annotated[str, Field(min_length=1, max_length=64)]
    expected_version: Version


class UpdateSettings(_Command):
    """Notification preferences. Only fields that are present change; jobs are replanned."""

    type: Literal["update_settings"] = "update_settings"
    expected_version: Version
    quiet_start: time | None = None
    quiet_end: time | None = None
    digest_weekday: int | None = Field(default=None, ge=0, le=6)
    digest_time: time | None = None
    default_notice_time: time | None = None
    evening_time: time | None = None
    later_minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    payment_advance_days: int | None = Field(default=None, ge=0, le=30)
    balance_threshold_days: int | None = Field(default=None, ge=0, le=60)
    overdue_followup_hours: int | None = Field(default=None, ge=1, le=168)
    max_auto_messages_per_day: int | None = Field(default=None, ge=1, le=20)
    profanity_enabled: bool | None = None


class UndoCommand(_Command):
    type: Literal["undo_command"] = "undo_command"
    target_command_id: Annotated[str, Field(max_length=128)] | None = None


class ConfirmPendingIntent(_Command):
    type: Literal["confirm_pending"] = "confirm_pending"
    pending_id: ID


class CancelPendingIntent(_Command):
    type: Literal["cancel_pending"] = "cancel_pending"
    pending_id: ID


Command = Annotated[
    CreateTask
    | UpdateTask
    | CompleteTask
    | CancelTask
    | RescheduleOccurrence
    | SkipOccurrence
    | CreateObligation
    | UpdateObligation
    | MarkPayment
    | ExtendCoverage
    | RecordTopup
    | CorrectBalance
    | ChangeDailyRate
    | ArchiveObligation
    | SnoozeNotification
    | ChangeTimezone
    | UpdateSettings
    | UndoCommand
    | ConfirmPendingIntent
    | CancelPendingIntent,
    Field(discriminator="type"),
]

COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)

# Commands whose effect can be reversed by a compensating undo.
UNDOABLE_TYPES = frozenset(
    {
        "create_task",
        "update_task",
        "complete_task",
        "cancel_task",
        "reschedule_occurrence",
        "skip_occurrence",
        "create_obligation",
        "update_obligation",
        "mark_payment",
        "extend_coverage",
        "record_topup",
        "correct_balance",
        "change_daily_rate",
        "archive_obligation",
    }
)
