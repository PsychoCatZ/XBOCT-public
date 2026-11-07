"""IntentProvider contract (docs/LLM_CONTRACT.md).

The provider only proposes. It references records by candidate IDs, expresses time as
typed expressions and money as decimal strings in major units. The application then
validates IDs against the candidates it sent, resolves dates, converts money, checks
versions and decides between apply, preview and clarification.

All objects forbid extra fields so the schema maps to strict JSON Schema later
(OpenAI strict tool, stage 3).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from xboct.domain.dates import DateExpr, Later, TimeExpr, When

MAX_MESSAGE_CHARS = 4000
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 12000
MAX_CANDIDATES = 30

Amount = Annotated[str, Field(min_length=1, max_length=20)]
CandidateId = Annotated[str, Field(min_length=1, max_length=32)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- request ---------------------------------------------------------------------------


class ContextMessage(_Model):
    role: Literal["user", "assistant"]
    text: str
    at: AwareDatetime


class CandidateRecord(_Model):
    """What the provider may reference. Notes are not sent: they are data, not commands."""

    id: str
    kind: Literal["task", "obligation"]
    title: str
    aliases: list[str] = Field(default_factory=list)
    obligation_kind: Literal["calendar", "coverage", "balance"] | None = None
    status: str
    version: int
    due: date | None = None


class IntentRequest(_Model):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    received_at: AwareDatetime
    timezone: str
    context: list[ContextMessage] = Field(default_factory=list, max_length=MAX_CONTEXT_MESSAGES)
    candidates: list[CandidateRecord] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    pending_preview: str | None = None
    """Summary of the one preview awaiting «да/нет», if any (code picks the target)."""


# --- proposed actions ------------------------------------------------------------------


class ProposedRecurrence(_Model):
    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    start: DateExpr
    interval: int = Field(default=1, ge=1, le=366)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=list)
    monthday: int | None = Field(default=None, ge=1, le=31)
    time: TimeExpr | None = None


class PCreateTask(_Model):
    type: Literal["create_task"] = "create_task"
    title: Annotated[str, Field(min_length=1, max_length=200)]
    note: str | None = None
    url: str | None = None
    next_action: str | None = None
    due: When | None = None
    recurrence: ProposedRecurrence | None = None


class PaidThrough(_Model):
    """What the owner said about the end of the paid period; the code derives the boundary.

    last_paid_day: «оплачено по 10.11 включительно» (the day itself is paid);
    first_unpaid_day: «с 11.11 снова платить» (the day itself is not paid);
    unclear: «оплачено до 10.11» without saying whether the day is paid (the code asks);
    month_only: «до января» without a day (the code asks). The model never computes the
    exclusive boundary itself.
    """

    kind: Literal["last_paid_day", "first_unpaid_day", "unclear", "month_only"]
    day: date | None = None
    month: int | None = Field(default=None, ge=1, le=12)

    @model_validator(mode="after")
    def _shape(self) -> PaidThrough:
        if self.kind == "month_only":
            if self.month is None:
                raise ValueError("month_only needs month")
        elif self.day is None:
            raise ValueError(f"{self.kind} needs a date")
        return self


class CoverageLength(_Model):
    unit: Literal["day", "week", "month", "year"]
    count: int = Field(ge=1, le=120)


class PaidPeriod(_Model):
    """A paid period whose start and length the owner stated («купил 10 октября на месяц»).

    The code computes the end by calendar months; the model never computes it. Not for a
    price period («19 долларов в месяц») and not for «оплачено с … по …», which is
    `paid_through`.
    """

    start: DateExpr
    length: CoverageLength


class PCreateObligation(_Model):
    """A new subscription (coverage) or calendar payment. Never a balance account (form).

    Always shown to the owner as a saved preview first. `amount` is as said; `currency`
    only when the owner named it («рубли», «доллары», «евро», ₽ $ €); «в месяц» is the
    price period (`price_period`), not a recurrence of a subscription. For kind=coverage the
    paid end is `paid_through` (an end the owner named) or `paid_period` (a start and a
    length the owner stated); the code computes the boundary. `recurrence` (with a known
    start) is for kind=calendar. `first_payment_on` is a payment date the owner named; it
    is kept in the note only. `allow_duplicate` only when the owner explicitly asked for a
    second record of the same name.
    """

    type: Literal["create_obligation"] = "create_obligation"
    title: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Literal["calendar", "coverage"]
    amount: Amount | None = None
    currency: Literal["RUB", "USD", "EUR"] | None = None
    price_period: Literal["day", "week", "month", "year"] | None = None
    paid_through: PaidThrough | None = None
    paid_period: PaidPeriod | None = None
    recurrence: ProposedRecurrence | None = None
    first_payment_on: DateExpr | None = None
    aliases: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    note: str | None = None
    allow_duplicate: bool = False


class PUpdateTask(_Model):
    type: Literal["update_task"] = "update_task"
    task_id: CandidateId
    title: str | None = None
    note: str | None = None
    url: str | None = None
    next_action: str | None = None
    due: When | None = None
    clear: list[Literal["note", "url", "next_action", "due"]] = Field(default_factory=list)


class PCompleteTask(_Model):
    type: Literal["complete_task"] = "complete_task"
    task_id: CandidateId


class PCancelTask(_Model):
    type: Literal["cancel_task"] = "cancel_task"
    task_id: CandidateId


class PeriodRef(_Model):
    """«октябрь» -> month; «за 15 сентября» -> date; nothing said -> current."""

    kind: Literal["current", "month", "date"]
    year: int | None = Field(default=None, ge=2000, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    day: date | None = None

    @model_validator(mode="after")
    def _shape(self) -> PeriodRef:
        if self.kind == "month" and self.month is None:
            raise ValueError("month period needs month")
        if self.kind == "date" and self.day is None:
            raise ValueError("date period needs date")
        return self


class PMarkPayment(_Model):
    type: Literal["mark_payment"] = "mark_payment"
    obligation_id: CandidateId
    periods: list[PeriodRef] = Field(min_length=1, max_length=24)
    amount: Amount | None = None
    paid_on: DateExpr | None = None


class CoverageUntil(_Model):
    """An exact first unpaid date, or a vague month («до января») that needs a question."""

    kind: Literal["date", "month_only"]
    day: date | None = None
    year: int | None = Field(default=None, ge=2000, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)


class PExtendCoverage(_Model):
    type: Literal["extend_coverage"] = "extend_coverage"
    obligation_id: CandidateId
    length: CoverageLength | None = None
    until: CoverageUntil | None = None
    start: DateExpr | None = None
    amount: Amount | None = None


class PRecordTopup(_Model):
    type: Literal["record_topup"] = "record_topup"
    obligation_id: CandidateId
    amount: Amount
    on: DateExpr | None = None
    at_time: TimeExpr | None = None
    start_from_zero: bool = False


class PCorrectBalance(_Model):
    type: Literal["correct_balance"] = "correct_balance"
    obligation_id: CandidateId
    amount: Amount
    on: DateExpr | None = None
    at_time: TimeExpr | None = None


class PChangeDailyRate(_Model):
    type: Literal["change_daily_rate"] = "change_daily_rate"
    obligation_id: CandidateId
    rate_per_day: Amount
    on: DateExpr | None = None


class PSnooze(_Model):
    type: Literal["snooze_notification"] = "snooze_notification"
    task_id: CandidateId | None = None
    obligation_id: CandidateId | None = None
    until: When | Later

    @model_validator(mode="after")
    def _one_subject(self) -> PSnooze:
        if (self.task_id is None) == (self.obligation_id is None):
            raise ValueError("exactly one of task_id / obligation_id")
        return self


class PSkipOccurrence(_Model):
    type: Literal["skip_occurrence"] = "skip_occurrence"
    obligation_id: CandidateId | None = None
    task_id: CandidateId | None = None
    period: PeriodRef


class PArchiveObligation(_Model):
    type: Literal["archive_obligation"] = "archive_obligation"
    obligation_id: CandidateId
    outstanding: Literal["keep_open", "close"] | None = None


class PUndo(_Model):
    type: Literal["undo_command"] = "undo_command"


ProposedAction = Annotated[
    PCreateTask
    | PCreateObligation
    | PUpdateTask
    | PCompleteTask
    | PCancelTask
    | PMarkPayment
    | PExtendCoverage
    | PRecordTopup
    | PCorrectBalance
    | PChangeDailyRate
    | PSnooze
    | PSkipOccurrence
    | PArchiveObligation
    | PUndo,
    Field(discriminator="type"),
]


class Clarification(_Model):
    question: Annotated[str, Field(min_length=1, max_length=500)]
    candidate_ids: list[CandidateId] = Field(default_factory=list, max_length=MAX_CANDIDATES)


class QuerySpec(_Model):
    kind: Literal["agenda", "payments", "balance"]
    period: Literal[
        "all",
        "today",
        "tomorrow",
        "this_week",
        "next_week",
        "this_month",
        "next_month",
        "month",
        "range",
    ] = "all"
    payment_status: Literal["all", "unpaid", "paid", "covered"] = "all"
    month: int | None = Field(default=None, ge=1, le=12)
    year: int | None = Field(default=None, ge=2000, le=2100)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _period_shape(self) -> QuerySpec:
        if self.kind != "payments" and (self.period != "all" or self.payment_status != "all"):
            raise ValueError("period/status filters currently belong to payment queries")
        if self.period == "month" and self.month is None:
            raise ValueError("named month requires month")
        if self.period == "range" and (
            self.start_date is None or self.end_date is None or self.start_date > self.end_date
        ):
            raise ValueError("date range requires ordered start_date and end_date")
        return self


class IntentEnvelope(_Model):
    """What the model proposes. It never applies anything itself.

    kind: actions -> proposed commands; query -> read-only answer; clarification -> a
    question; confirm_pending / cancel_pending -> the owner answered «да» / «нет» to the
    current preview (the code decides which preview that is, and checks it is current).
    """

    kind: Literal[
        "actions", "query", "clarification", "confirm_pending", "cancel_pending", "conversation"
    ]
    actions: list[ProposedAction] = Field(default_factory=list, max_length=20)
    query: QuerySpec | None = None
    clarification: Clarification | None = None
    requires_confirmation: bool = False
    conversation_kind: (
        Literal["greeting", "thanks", "frustration", "capabilities", "acknowledgement"] | None
    ) = None

    @model_validator(mode="after")
    def _consistent(self) -> IntentEnvelope:
        if self.kind == "actions" and not self.actions:
            raise ValueError("actions envelope without actions")
        if self.kind == "query" and self.query is None:
            raise ValueError("query envelope without query")
        if self.kind == "clarification" and self.clarification is None:
            raise ValueError("clarification envelope without question")
        if self.kind == "conversation" and (
            self.conversation_kind is None
            or self.query is not None
            or self.clarification is not None
        ):
            raise ValueError("conversation needs a conversational intent without factual query")
        if self.kind != "conversation" and self.conversation_kind is not None:
            raise ValueError("conversation intent only in conversation envelope")
        if self.kind != "actions" and self.actions:
            raise ValueError("actions only in actions envelope")
        return self
