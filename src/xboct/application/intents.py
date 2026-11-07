"""Free-text pipeline: provider proposal -> validation -> resolution -> apply or preview.

1. A repeated command_id returns the stored result (no second provider call).
2. Candidates are read in a short transaction; the provider runs outside any
   transaction with a timeout.
3. One write transaction: every referenced ID must be one of the candidates sent,
   relative dates/periods/money are resolved by code, expected versions come from the
   candidate snapshot (a record changed meanwhile -> version conflict, nothing applied).
4. Mutations of one subject are applied at once. Two or more distinct targets of the
   resolved commands (not mentions in the text), archive or an explicit provider request
   -> one preview stored as a pending intent (30 min, newest only). Queries never ask.
5. Date ambiguity («на следующей неделе» without a day) is a clarification raised during
   resolution here, so the fixture and any future provider follow the same rule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, literal_column, select, update

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult, Outcome
from xboct.application.duplicates import find_duplicate, is_duplicate, same_name
from xboct.application.handlers import dispatch, period_text, pick_current_occurrence
from xboct.application.materialize import ensure_materialized
from xboct.application.payment_queries import payment_query_text
from xboct.application.queries import (
    agenda,
    agenda_text,
    balance_forecasts,
    candidates,
)
from xboct.application.render import (
    MONTHS_GEN,
    MONTHS_NOM,
    fmt_date,
    fmt_instant,
    fmt_money,
    forecast_text,
    plural,
)
from xboct.application.service import Application, payload_hash
from xboct.application.uow import Uow
from xboct.domain.coverage import extend_coverage, paid_through
from xboct.domain.dates import (
    ResolveContext,
    When,
    resolve_date,
    resolve_event_instant,
    resolve_reminder_instant,
    resolve_time,
    resolve_when,
)
from xboct.domain.errors import NeedsClarification, ValidationFailed
from xboct.domain.money import parse_major
from xboct.domain.timeutil import get_zone, last_day_of_month, local_date
from xboct.llm import contract as p
from xboct.llm.contract import MAX_MESSAGE_CHARS, CandidateRecord, ContextMessage
from xboct.llm.provider import IntentProvider, IntentProviderError, ProviderMeta
from xboct.storage.models import (
    CommandReceipt,
    LlmCall,
    Obligation,
    Occurrence,
    PendingIntent,
    Recurrence,
    Task,
)

log = logging.getLogger(__name__)

INTENT_TIMEOUT_SECONDS = 20.0
DEFAULT_DAILY_CALL_LIMIT = 50
DAILY_LIMIT_TEXT = (
    "Дневной лимит разбора сообщений моделью исчерпан; данные не менял. "
    "Команды, кнопки и напоминания работают."
)
PENDING_TTL = timedelta(minutes=30)
FAILURE_TEXT = "Не разобрал сообщение; данные не менял. Можно сделать через форму."


class IntentService:
    def __init__(
        self,
        app: Application,
        provider: IntentProvider,
        *,
        timeout_seconds: float | None = None,
        daily_call_limit: int = DEFAULT_DAILY_CALL_LIMIT,
    ) -> None:
        self.app = app
        self.provider = provider
        # One attempt plus at most one retry inside the adapter, then a hard stop.
        attempt = getattr(provider, "attempt_timeout", INTENT_TIMEOUT_SECONDS)
        self.timeout_seconds = timeout_seconds or attempt * 2 + 5
        self.daily_call_limit = daily_call_limit

    async def handle_message(
        self,
        text: str,
        ctx: CommandContext,
        context: Sequence[ContextMessage] = (),
    ) -> CommandResult:
        digest = payload_hash({"message": text})
        async with self.app.sessions() as session:
            receipt = await session.get(CommandReceipt, ctx.command_id)
            if receipt is not None:
                return self.app._replay(receipt, digest)

        if len(text) > MAX_MESSAGE_CHARS:

            async def too_long(_: Uow) -> Outcome:
                raise ValidationFailed(
                    f"Сообщение длиннее {MAX_MESSAGE_CHARS} символов", code="message_too_long"
                )

            return await self.app.run(ctx, "message", digest, too_long)

        answer = short_answer(text)
        if answer is not None:  # decided by the code from the saved plan; no model call

            async def answer_plan(uow: Uow) -> Outcome:
                return await _answer_current_plan(uow, answer)

            return await self.app.run(ctx, "message", digest, answer_plan)

        # Short read transaction; nothing is held while the model thinks.
        snap = await self.app.read(lambda uow: _snapshot(uow, text))
        if snap.calls_today >= self.daily_call_limit:
            return self._failure(ctx, "llm_daily_limit", DAILY_LIMIT_TEXT)
        request = p.IntentRequest(
            message=text,
            received_at=ctx.received_at,
            timezone=snap.timezone,
            context=bounded_context(context),
            candidates=snap.candidates,
            pending_preview=snap.pending_summary,
        )
        started = time.monotonic()
        meta: ProviderMeta | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if hasattr(self.provider, "propose_with_meta"):
                    envelope, meta = await self.provider.propose_with_meta(request)
                else:  # minimal providers only implement propose()
                    envelope = await self.provider.propose(request)
        except IntentProviderError as exc:
            await self._log_call(ctx, exc.meta, started, error=exc.kind)
            return self._failure(ctx, f"llm_{exc.kind}")
        except TimeoutError:
            await self._log_call(ctx, None, started, error="timeout")
            return self._failure(ctx, "llm_timeout")
        except Exception:  # a broken adapter must not look like success
            log.exception("intent provider failed", extra={"command_id": ctx.command_id})
            await self._log_call(ctx, None, started, error="unavailable")
            return self._failure(ctx, "llm_unavailable")
        await self._log_call(ctx, meta, started, error=None)

        # Fresh write transaction: versions seen by the model are re-checked here, so a
        # record changed while the model was thinking is never overwritten.
        by_id = {cand.id: cand for cand in snap.candidates}
        return await self.app.run(
            ctx,
            "message",
            digest,
            lambda uow: apply_envelope(uow, envelope, by_id, pending_id=snap.pending_id),
        )

    async def _log_call(
        self,
        ctx: CommandContext,
        meta: ProviderMeta | None,
        started: float,
        *,
        error: str | None,
    ) -> None:
        provider_name = getattr(self.provider, "name", type(self.provider).__name__)
        async with self.app.sessions() as session, session.begin():
            settings = await self.app.settings_in(session)
            now = self.app.clock.now()
            session.add(
                LlmCall(
                    at=now,
                    local_date=local_date(now, get_zone(settings.timezone)),
                    command_id=ctx.command_id,
                    provider=meta.provider if meta else provider_name,
                    model=meta.model if meta else None,
                    prompt_version=meta.prompt_version if meta else None,
                    status="error" if error else "ok",
                    error_kind=error,
                    attempts=meta.attempts if meta else 1,
                    input_tokens=meta.input_tokens if meta else None,
                    output_tokens=meta.output_tokens if meta else None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )

    @staticmethod
    def _failure(ctx: CommandContext, code: str, text: str = FAILURE_TEXT) -> CommandResult:
        # Not stored as a receipt: the same update may be retried when the model is back.
        return CommandResult(
            command_id=ctx.command_id,
            command_type="message",
            status="rejected",
            code=code,
            message=text,
        )


@dataclass(frozen=True)
class _Snapshot:
    candidates: list[CandidateRecord]
    timezone: str
    calls_today: int
    pending_id: str | None
    pending_summary: str | None


async def _snapshot(uow: Uow, text: str) -> _Snapshot:
    pending = await current_pending(uow)
    calls = await uow.session.scalar(
        select(func.coalesce(func.sum(LlmCall.attempts), 0)).where(
            LlmCall.local_date == uow.today_now
        )
    )
    return _Snapshot(
        candidates=await candidates(uow, text),
        timezone=uow.settings.timezone,
        calls_today=int(calls or 0),
        pending_id=pending.id if pending else None,
        pending_summary=pending.summary if pending else None,
    )


async def _no_pending_text(uow: Uow) -> str:
    """«Да»/«нет» with nothing to answer: say what the stored state really is."""
    latest = await uow.session.scalar(
        select(PendingIntent)
        .order_by(PendingIntent.created_at.desc(), literal_column("rowid").desc())
        .limit(1)
    )
    if latest is None:
        return "Сохранённого предложения нет. Напиши, что добавить."
    if latest.status == "pending":  # not current, so it has expired
        return "Предложение устарело. Напиши, что добавить, ещё раз."
    if latest.status == "cancelled":
        return "Предложение уже отменено. Напиши, что добавить, если нужно."
    if latest.status == "superseded":
        return "Предыдущее предложение заменено и не действует. Напиши, что добавить, ещё раз."
    return "Это предложение уже применено. Напиши, что добавить."


_YES = frozenset(
    {"да", "подтверждаю", "подтвердить", "применяй", "применить", "верно", "все верно"}
    | {"да подтверждаю", "да верно", "да применяй"}
)
_NO = frozenset(
    {"нет", "не надо", "не нужно", "отмена", "отмени", "отменить", "отменяю", "не подтверждаю"}
    | {"нет не надо", "нет отмена"}
)


def short_answer(text: str) -> Literal["confirm", "cancel"] | None:
    """An unambiguous one-word «да»/«нет». The state of the saved plan decides what happens,
    not a model's choice of `kind`; anything else (chat, «ок», «давай») still goes to the
    model, which cannot confirm what it does not see as a plan."""
    words = "".join(ch if ch.isalnum() else " " for ch in text.casefold().replace("ё", "е"))
    normal = " ".join(words.split())
    if normal in _YES:
        return "confirm"
    if normal in _NO:
        return "cancel"
    return None


async def _answer_current_plan(uow: Uow, answer: Literal["confirm", "cancel"]) -> Outcome:
    current = await current_pending(uow)
    if current is None:
        raise NeedsClarification(await _no_pending_text(uow), code="no_current_pending")
    command: c.Command = (
        c.ConfirmPendingIntent(pending_id=current.id)
        if answer == "confirm"
        else c.CancelPendingIntent(pending_id=current.id)
    )
    return await dispatch(uow, command)


async def _supersede_current(uow: Uow) -> None:
    """An attempt to act ended in a question: the older plan must not stay confirmable (a
    following «да» would answer the question, not the plan)."""
    await uow.session.execute(
        update(PendingIntent)
        .where(PendingIntent.status == "pending")
        .values(status="superseded", resolved_at=uow.now, resolved_by=uow.ctx.command_id)
    )


async def current_pending(uow: Uow) -> PendingIntent | None:
    return await uow.session.scalar(
        select(PendingIntent)
        .where(PendingIntent.status == "pending", PendingIntent.expires_at > uow.now)
        .order_by(PendingIntent.created_at.desc(), literal_column("rowid").desc())
        .limit(1)
    )


def bounded_context(messages: Sequence[ContextMessage]) -> list[ContextMessage]:
    """Last 20 messages, at most 12000 characters, oldest dropped first."""
    kept: list[ContextMessage] = []
    total = 0
    for message in reversed(list(messages)[-p.MAX_CONTEXT_MESSAGES :]):
        if total + len(message.text) > p.MAX_CONTEXT_CHARS:
            break
        kept.append(message)
        total += len(message.text)
    return list(reversed(kept))


# --- envelope -> outcome --------------------------------------------------------------


async def apply_envelope(
    uow: Uow,
    envelope: p.IntentEnvelope,
    by_id: dict[str, CandidateRecord],
    *,
    pending_id: str | None = None,
) -> Outcome:
    if envelope.kind in ("actions", "clarification"):
        uow.on_clarification.append(lambda: _supersede_current(uow))
    if envelope.kind in ("confirm_pending", "cancel_pending"):
        current = await current_pending(uow)
        if current is None:
            raise NeedsClarification(await _no_pending_text(uow), code="no_current_pending")
        if current.id != pending_id:
            raise NeedsClarification(
                "Предложение сменилось, пока я думал. Напиши, что сделать, ещё раз.",
                code="no_current_pending",
            )
        command: c.Command = (
            c.ConfirmPendingIntent(pending_id=current.id)
            if envelope.kind == "confirm_pending"
            else c.CancelPendingIntent(pending_id=current.id)
        )
        return await dispatch(uow, command)
    if envelope.kind == "clarification":
        assert envelope.clarification is not None
        options = [
            {"id": cid, "title": by_id[cid].title}
            for cid in envelope.clarification.candidate_ids
            if cid in by_id
        ]
        raise NeedsClarification(
            envelope.clarification.question, code="provider_clarification", options=options
        )
    if envelope.kind == "query":
        assert envelope.query is not None
        return Outcome(await answer_query(uow, envelope.query), status="answered", undoable=False)

    if envelope.kind == "conversation":
        replies = {
            "greeting": "Привет, Кот ) Какие дела или платежи разберём?",
            "thanks": "Мур )",
            "frustration": (
                "Да, отвечаю слишком механически ) «Что оплачено» — текущие оплаченные сроки "
                "сервисов, а «что оплатил» — история платежей. Не буду снова кидать весь список."
            ),
            "capabilities": (
                "Могу записать дело, подписку или платёж по календарю (покажу, что сохраню, "
                "и спрошу «да»), учесть оплату и пополнение, перенести напоминание и показать, "
                "что требует внимания. VPS с балансом заводи в форме."
            ),
            "acknowledgement": "Понял )",
        }
        assert envelope.conversation_kind is not None
        return Outcome(replies[envelope.conversation_kind], status="answered", undoable=False)

    commands = [await translate(uow, action, by_id) for action in envelope.actions]
    _reject_twins_in_batch(commands)
    subjects = {_subject_key(cmd, i) for i, cmd in enumerate(commands)}
    needs_preview = (
        envelope.requires_confirmation
        or len(subjects) > 1
        or any(cmd.type in ("archive_obligation", "create_obligation") for cmd in commands)
    )
    if needs_preview:
        return await create_pending(uow, commands, by_id)

    outcomes = [await dispatch(uow, cmd) for cmd in commands]
    return Outcome(
        " ".join(o.message for o in outcomes),
        {"results": [o.data for o in outcomes]},
        undoable=all(o.undoable for o in outcomes),
    )


async def answer_query(uow: Uow, query: p.QuerySpec) -> str:
    if query.kind == "agenda":
        return agenda_text(uow, await agenda(uow))
    if query.kind == "payments":
        return await payment_query_text(uow, query)
    rows = await balance_forecasts(uow)
    if not rows:
        return "Обязательств с балансом нет."
    return "\n".join(
        f"«{ob.title}»: {forecast_text(fc, ob.currency, uow.tz, uow.today_now)}" for ob, fc in rows
    )


def _reject_twins_in_batch(commands: list[c.Command]) -> None:
    """The same new name twice in one request without the owner asking for a second record:
    ask before a preview exists (the applying transaction refuses it as well)."""
    created: list[c.CreateObligation] = []
    for cmd in commands:
        if not isinstance(cmd, c.CreateObligation):
            continue
        if cmd.duplicates == "forbid":
            for earlier in created:
                if same_name(earlier.title) == same_name(cmd.title) or any(
                    _extends(a, b)
                    for a, b in ((cmd.title, earlier.title), (earlier.title, cmd.title))
                ):
                    raise NeedsClarification(
                        f"В запросе дважды «{cmd.title}». Создать одну запись или две? "
                        f"Напиши, что сделать.",
                        code="duplicate_in_request",
                    )
        created.append(cmd)


def _extends(longer: str, shorter: str) -> bool:
    return same_name(longer).startswith(same_name(shorter) + " ")


def _subject_key(cmd: c.Command, index: int) -> tuple[str, ...]:
    if isinstance(cmd, c.CreateTask | c.CreateObligation):
        return ("new", str(index))
    if isinstance(cmd, c.UpdateTask | c.CompleteTask | c.CancelTask):
        return ("task", cmd.task_id)
    if isinstance(cmd, c.SnoozeNotification):
        return (cmd.subject_type, cmd.subject_id)
    if isinstance(cmd, c.SkipOccurrence | c.RescheduleOccurrence):
        return ("occurrence", cmd.occurrence_id)
    obligation_id = getattr(cmd, "obligation_id", None)
    if obligation_id is not None:
        return ("obligation", obligation_id)
    return (cmd.type,)


async def create_pending(
    uow: Uow, commands: list[c.Command], by_id: dict[str, CandidateRecord]
) -> Outcome:
    await uow.session.execute(
        update(PendingIntent)
        .where(PendingIntent.status == "pending")
        .values(status="superseded", resolved_at=uow.now, resolved_by=uow.ctx.command_id)
    )
    lines = [f"{i}. {await describe(uow, cmd)}" for i, cmd in enumerate(commands, 1)]
    expected: dict[str, int] = {}
    referenced: set[str] = set()
    for cmd in commands:
        version = getattr(cmd, "expected_version", None)
        for key in ("task_id", "obligation_id", "occurrence_id", "subject_id"):
            target = getattr(cmd, key, None)
            if target:
                referenced.add(target)
                if version is not None:
                    expected[target] = version
        for ref in getattr(cmd, "occurrences", None) or []:
            expected[ref.id] = ref.expected_version
    pending = PendingIntent(
        command_id=uow.ctx.command_id,
        actions=[cmd.model_dump(mode="json") for cmd in commands],
        candidates=[by_id[k].model_dump(mode="json") for k in sorted(referenced) if k in by_id],
        expected_versions=expected,
        summary="\n".join(lines),
        status="pending",
        created_at=uow.now,
        expires_at=uow.now + PENDING_TTL,
    )
    uow.session.add(pending)
    await uow.session.flush()
    text = (
        "Проверь, что применяю — всё вместе или ничего:\n"
        + "\n".join(lines)
        + f"\nПодтвердить? Предложение действует до "
        f"{fmt_instant(pending.expires_at, uow.tz, uow.today_now)}."
    )
    return Outcome(
        text,
        {
            "pending_id": pending.id,
            "expires_at": pending.expires_at.isoformat(),
            "actions": pending.actions,
        },
        status="preview",
        undoable=False,
    )


async def _title(uow: Uow, model: type[Task] | type[Obligation], entity_id: str) -> str:
    entity = await uow.session.get(model, entity_id)
    return entity.title if entity is not None else entity_id


async def describe(uow: Uow, cmd: c.Command) -> str:
    today = uow.today
    match cmd:
        case c.CreateTask():
            due = f", срок {fmt_date(cmd.due_date, today)}" if cmd.due_date is not None else ""
            return f"новое дело «{cmd.title}»{due}"
        case c.UpdateTask():
            return f"«{await _title(uow, Task, cmd.task_id)}»: изменить"
        case c.CompleteTask():
            return f"«{await _title(uow, Task, cmd.task_id)}»: выполнено"
        case c.CancelTask():
            return f"«{await _title(uow, Task, cmd.task_id)}»: отменить дело"
        case c.MarkPayment():
            ob = await uow.get(Obligation, cmd.obligation_id)
            periods = []
            for ref in cmd.occurrences:
                occ = await uow.get(Occurrence, ref.id)
                periods.append(
                    f"{period_text(uow, occ)} (срок {fmt_date(occ.effective_due, today)})"
                )
            return f"«{ob.title}»: оплачено за " + ", ".join(periods)
        case c.ExtendCoverage():
            return f"«{await _title(uow, Obligation, cmd.obligation_id)}»: продлить оплату"
        case c.RecordTopup():
            ob = await uow.get(Obligation, cmd.obligation_id)
            return f"«{ob.title}»: пополнение на {fmt_money(cmd.amount_minor, ob.currency)}"
        case c.CorrectBalance():
            ob = await uow.get(Obligation, cmd.obligation_id)
            return f"«{ob.title}»: остаток {fmt_money(cmd.amount_minor, ob.currency)}"
        case c.ChangeDailyRate():
            ob = await uow.get(Obligation, cmd.obligation_id)
            return f"«{ob.title}»: расход {fmt_money(cmd.rate_minor_per_day, ob.currency)}/день"
        case c.ArchiveObligation():
            title = await _title(uow, Obligation, cmd.obligation_id)
            return f"«{title}»: в архив, будущие периоды не планировать"
        case c.SkipOccurrence():
            occ = await uow.get(Occurrence, cmd.occurrence_id)
            model = Task if occ.owner_type == "task" else Obligation
            return f"«{await _title(uow, model, occ.owner_id)}»: пропустить {period_text(uow, occ)}"
        case c.SnoozeNotification():
            model = Task if cmd.subject_type == "task" else Obligation
            title = await _title(uow, model, cmd.subject_id)
            return f"«{title}»: напомнить {fmt_instant(cmd.until, uow.tz, today)}"
        case c.CreateObligation():
            twin = await find_duplicate(uow, cmd.title) if cmd.duplicates == "allow" else None
            return _describe_new_obligation(cmd, today, twin.title if twin else None)
        case c.UndoCommand():
            return "отменить последнее действие"
    return cmd.type


_EVERY = {
    "daily": ("день", "дня", "дней"),
    "weekly": ("неделю", "недели", "недель"),
    "monthly": ("месяц", "месяца", "месяцев"),
    "yearly": ("год", "года", "лет"),
}
_EVERY_ONE = {
    "daily": "каждый день",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
    "yearly": "каждый год",
}


def _every(rule: c.RecurrenceSpec) -> str:
    if rule.interval == 1:
        return _EVERY_ONE[rule.frequency]
    forms = _EVERY[rule.frequency]
    return f"каждые {rule.interval} {plural(rule.interval, *forms)}"


def _describe_new_obligation(cmd: c.CreateObligation, today: date, twin: str | None) -> str:
    """What will be saved, built from the typed command (never from the model's words)."""
    if cmd.kind == "coverage":
        head = f"новая подписка «{cmd.title}»"
        if cmd.coverage_until is not None:
            last = fmt_date(paid_through(cmd.coverage_until), today)
            facts = [
                f"оплачено по {last} включительно",
                f"следующая оплата с {fmt_date(cmd.coverage_until, today)}",
            ]
        else:
            facts = ["граница оплаты пока неизвестна"]
    else:
        head = f"новый платёж по календарю «{cmd.title}»"
        rule = cmd.recurrence
        facts = [f"{_every(rule)}, первый срок {fmt_date(rule.anchor_date, today)}"] if rule else []
    if cmd.amount_minor is not None:
        facts.append(f"сумма {fmt_money(cmd.amount_minor, cmd.currency)}")
    if cmd.note:
        saved = "в заметку запишу: «" + cmd.note.strip()[:300].replace("\n", " · ") + "»"
        if FIRST_PAYMENT_PREFIX in cmd.note:
            saved += " (записи об оплате не создаю, дата останется только в заметке)"
        facts.append(saved)
    if twin is not None:
        facts.append(f"«{twin}» уже есть — по твоей просьбе создаю вторую запись")
    return f"{head}: " + "; ".join(facts)


# --- translation of single proposals --------------------------------------------------


def _ref(by_id: dict[str, CandidateRecord], ref_id: str, kind: str) -> CandidateRecord:
    record = by_id.get(ref_id)
    if record is None or record.kind != kind:
        raise ValidationFailed(
            "Предложение ссылается на запись, которой не было среди кандидатов",
            code="unknown_reference",
            ref=ref_id,
        )
    return record


def _resolve_ctx(uow: Uow) -> ResolveContext:
    s = uow.settings
    return ResolveContext(
        received_at=uow.ctx.received_at,
        tz=uow.tz,
        evening=s.evening_time,
        notice_time=s.default_notice_time,
        later_minutes=s.later_minutes,
    )


def _build(model: type[Any], **fields: Any) -> Any:
    try:
        return model(**fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValidationFailed(
            f"Некорректные данные в предложении: {first.get('msg', 'ошибка')}",
            code="invalid_proposal",
        ) from exc


def _amount(text: str, currency: str) -> int:
    return parse_major(text, currency)


async def _occurrence_for(
    uow: Uow, owner_type: str, owner_id: str, title: str, period: p.PeriodRef
) -> Occurrence:
    if period.kind == "current":
        return await pick_current_occurrence(uow, owner_type, owner_id, title)
    if period.kind == "month":
        assert period.month is not None
        year = period.year or _nearest_year(uow.today, period.month)
        start = date(year, period.month, 1)
        end = date(year, period.month, last_day_of_month(year, period.month))
    else:
        assert period.day is not None
        start = end = period.day
    owner_model = Task if owner_type == "task" else Obligation
    owner = await uow.get(owner_model, owner_id)
    if owner.recurrence_id is not None:
        await ensure_materialized(uow, await uow.get(Recurrence, owner.recurrence_id), end)
    rows = list(
        await uow.session.scalars(
            select(Occurrence)
            .where(
                Occurrence.owner_type == owner_type,
                Occurrence.owner_id == owner_id,
                Occurrence.nominal_due >= start,
                Occurrence.nominal_due <= end,
                Occurrence.status != "superseded",
            )
            .order_by(Occurrence.nominal_due)
        )
    )
    if len(rows) == 1:
        return rows[0]
    label = (
        f"{MONTHS_NOM[start.month - 1]} {start.year}"
        if period.kind == "month"
        else fmt_date(start, uow.today)
    )
    if not rows:
        raise NeedsClarification(
            f"У «{title}» нет периода за {label}. Какой период имеешь в виду?",
            code="period_not_found",
        )
    raise NeedsClarification(
        f"У «{title}» несколько периодов за {label}. Какой именно?",
        code="ambiguous_period",
        options=[{"occurrence_id": o.id, "due": o.nominal_due.isoformat()} for o in rows],
    )


def _nearest_year(today: date, month: int) -> int:
    """«Октябрь» without a year: this year's, unless that is far in the future."""
    if month - today.month > 6:
        return today.year - 1
    if today.month - month > 6:
        return today.year + 1
    return today.year


FIRST_PAYMENT_PREFIX = "Первая оплата:"
_PRICE_PERIOD = {"day": "день", "week": "неделю", "month": "месяц", "year": "год"}
_PERIOD_UNIT = {
    "day": lambda n: plural(n, "день", "дня", "дней"),
    "week": lambda n: plural(n, "неделю", "недели", "недель"),
    "month": lambda n: plural(n, "месяц", "месяца", "месяцев"),
    "year": lambda n: plural(n, "год", "года", "лет"),
}
_KIND_LABEL = {"calendar": "платёж по календарю", "coverage": "подписка", "balance": "баланс"}


async def _translate_create_obligation(
    uow: Uow, action: p.PCreateObligation, rc: ResolveContext
) -> c.CreateObligation:
    """The owner's typed proposal -> a command. Everything the owner did not say stays a
    question; the exclusive coverage boundary is computed here, not by the model."""
    title = action.title.strip()
    if action.kind == "coverage" and action.recurrence is not None:
        raise ValidationFailed(
            "Некорректные данные в предложении: у подписки нет правила повтора",
            code="invalid_proposal",
        )
    if action.kind == "calendar" and (
        action.paid_through is not None or action.paid_period is not None
    ):
        raise ValidationFailed(
            "Некорректные данные в предложении: у платежа по календарю нет границы оплаты",
            code="invalid_proposal",
        )

    currency = action.currency or "RUB"
    amount_minor = None
    if action.amount is not None:
        if action.currency is None:
            raise NeedsClarification(
                f"Уточни валюту: {action.amount} — рубли, доллары или евро?",
                code="currency_unclear",
            )
        amount_minor = _amount(action.amount, currency)

    coverage_until: date | None = None
    recurrence: c.RecurrenceSpec | None = None
    period_line: str | None = None
    if action.kind == "coverage":
        through = action.paid_through
        period = action.paid_period
        named_end: date | None = None
        if through is not None:
            if through.kind == "month_only":
                month = MONTHS_GEN[through.month - 1] if through.month else "месяца"
                raise NeedsClarification(
                    f"«До {month}» можно понять по-разному. Назови точную дату: последний "
                    f"оплаченный день включительно.",
                    code="coverage_boundary_vague",
                )
            assert through.day is not None
            if through.kind == "unclear":
                raise NeedsClarification(
                    f"«До {fmt_date(through.day, rc.today)}»: этот день уже оплачен или с него "
                    f"снова нужно платить?",
                    code="coverage_boundary_unclear",
                )
            named_end = (
                through.day + timedelta(days=1) if through.kind == "last_paid_day" else through.day
            )
        computed_end: date | None = None
        if period is not None:
            start = resolve_date(period.start, rc.today)
            try:
                computed_end = extend_coverage(start, period.length.unit, period.length.count)
            except ValidationFailed as exc:
                raise NeedsClarification(
                    f"Не получилось посчитать оплаченный период «{title}»: {exc.message}. "
                    f"Назови последний оплаченный день.",
                    code="paid_period_invalid",
                ) from exc
            length = (
                f"{period.length.count} {_PERIOD_UNIT[period.length.unit](period.length.count)}"
            )
            if named_end is not None and named_end != computed_end:
                raise NeedsClarification(
                    f"Ты назвал два срока: с {fmt_date(start, rc.today)} на {length} — это "
                    f"по {fmt_date(paid_through(computed_end), rc.today)} включительно, а "
                    f"отдельно по {fmt_date(paid_through(named_end), rc.today)} включительно. "
                    f"Какой верен?",
                    code="paid_period_conflict",
                )
            if named_end is None and computed_end < rc.today:
                raise NeedsClarification(
                    f"С {fmt_date(start, rc.today)} на {length} оплата закончилась "
                    f"{fmt_date(paid_through(computed_end), rc.today)} включительно. "
                    f"Подписку с тех пор продлевали? Назови последний оплаченный день.",
                    code="paid_period_elapsed",
                )
            period_line = (
                f"Оплачено с {fmt_date(start)} на {length}, "
                f"по {fmt_date(paid_through(computed_end))} включительно"
            )
        coverage_until = named_end if named_end is not None else computed_end
        if coverage_until is None:
            known = ""
            if action.first_payment_on is not None:
                first = fmt_date(resolve_date(action.first_payment_on, rc.today), rc.today)
                known = f" Знаю только, что первая оплата была {first}."
            raise NeedsClarification(
                f"До какого дня «{title}» уже оплачена? Назови последний оплаченный день.{known}",
                code="paid_through_missing",
            )
    else:
        rule = action.recurrence
        if rule is None:
            raise NeedsClarification(
                f"Назови ближайший срок платежа «{title}» и как часто он повторяется.",
                code="recurrence_missing",
            )
        recurrence = _build(
            c.RecurrenceSpec,
            frequency=rule.frequency,
            anchor_date=resolve_date(rule.start, rc.today),
            interval=rule.interval,
            weekdays=rule.weekdays,
            monthday=rule.monthday,
            due_time=resolve_time(rule.time, rc) if rule.time is not None else None,
        )

    if not action.allow_duplicate:
        for ob in await uow.session.scalars(select(Obligation).where(Obligation.active)):
            if is_duplicate(title, ob):
                found = _KIND_LABEL[ob.kind]
                if ob.kind == "coverage" and ob.coverage_until is not None:
                    last = fmt_date(paid_through(ob.coverage_until), rc.today)
                    found += f", оплачено по {last} включительно"
                raise NeedsClarification(
                    f"«{ob.title}» уже есть ({found}). Создать вторую запись или изменить "
                    f"существующую? Напиши, что сделать.",
                    code="duplicate_obligation",
                    options=[{"id": ob.id, "title": ob.title}],
                )

    # Facts the owner stated that have no field of their own go to the note as lines built
    # by the code; the owner's own note stays first and untouched.
    lines: list[str] = []
    if action.note:
        lines.append(action.note)
    if amount_minor is not None and action.price_period is not None:
        price = fmt_money(amount_minor, currency).replace("\xa0", " ")  # a plain-text note
        lines.append(f"Цена: {price} в {_PRICE_PERIOD[action.price_period]}")
    if period_line is not None:
        lines.append(period_line)
    if action.first_payment_on is not None:
        first = fmt_date(resolve_date(action.first_payment_on, rc.today))
        lines.append(f"{FIRST_PAYMENT_PREFIX} {first} (со слов владельца)")
    return _build(
        c.CreateObligation,
        title=title,
        kind=action.kind,
        aliases=action.aliases,
        currency=currency,
        amount_minor=amount_minor,
        note="\n".join(lines) or None,
        recurrence=recurrence,
        coverage_until=coverage_until,
        duplicates="allow" if action.allow_duplicate else "forbid",
    )


async def translate(
    uow: Uow, action: p.ProposedAction, by_id: dict[str, CandidateRecord]
) -> c.Command:
    rc = _resolve_ctx(uow)
    match action:
        case p.PCreateTask():
            due_date = due_time = None
            if action.due is not None:
                due_date, due_time = resolve_when(action.due, rc)
            recurrence = None
            if action.recurrence is not None:
                r = action.recurrence
                recurrence = _build(
                    c.RecurrenceSpec,
                    frequency=r.frequency,
                    anchor_date=resolve_date(r.start, rc.today),
                    interval=r.interval,
                    weekdays=r.weekdays,
                    monthday=r.monthday,
                    due_time=resolve_time(r.time, rc) if r.time is not None else None,
                )
            return _build(
                c.CreateTask,
                title=action.title,
                note=action.note,
                url=action.url,
                next_action=action.next_action,
                due_date=due_date,
                due_time=due_time,
                recurrence=recurrence,
            )
        case p.PCreateObligation():
            return await _translate_create_obligation(uow, action, rc)
        case p.PUpdateTask():
            record = _ref(by_id, action.task_id, "task")
            fields: dict[str, Any] = {}
            for name in ("title", "note", "url", "next_action"):
                if getattr(action, name) is not None:
                    fields[name] = getattr(action, name)
            for name in action.clear:
                if name == "due":
                    fields["due_date"] = None
                    fields["due_time"] = None
                else:
                    fields[name] = None
            if action.due is not None:
                fields["due_date"], fields["due_time"] = resolve_when(action.due, rc)
            return _build(
                c.UpdateTask, task_id=record.id, expected_version=record.version, **fields
            )
        case p.PCompleteTask():
            record = _ref(by_id, action.task_id, "task")
            return c.CompleteTask(task_id=record.id, expected_version=record.version)
        case p.PCancelTask():
            record = _ref(by_id, action.task_id, "task")
            return c.CancelTask(task_id=record.id, expected_version=record.version)
        case p.PMarkPayment():
            record = _ref(by_id, action.obligation_id, "obligation")
            ob = await uow.get(Obligation, record.id)
            refs: list[c.OccurrenceRef] = []
            for period in action.periods:
                occ = await _occurrence_for(uow, "obligation", ob.id, ob.title, period)
                refs.append(c.OccurrenceRef(id=occ.id, expected_version=occ.version))
            return _build(
                c.MarkPayment,
                obligation_id=ob.id,
                occurrences=refs,
                amount_minor=_amount(action.amount, ob.currency) if action.amount else None,
                paid_at=resolve_event_instant(action.paid_on, None, rc),
            )
        case p.PExtendCoverage():
            record = _ref(by_id, action.obligation_id, "obligation")
            ob = await uow.get(Obligation, record.id)
            until = None
            if action.until is not None:
                if action.until.kind == "month_only" or action.until.day is None:
                    month = MONTHS_GEN[action.until.month - 1] if action.until.month else "месяца"
                    raise NeedsClarification(
                        f"«До {month}» можно понять по-разному. С какой точной даты нужно "
                        f"платить снова (или по какое число включительно оплачено)?",
                        code="coverage_boundary_vague",
                    )
                until = action.until.day
            return _build(
                c.ExtendCoverage,
                obligation_id=ob.id,
                expected_version=record.version,
                start=resolve_date(action.start, rc.today) if action.start else None,
                unit=action.length.unit if action.length else None,
                count=action.length.count if action.length else None,
                until=until,
                amount_minor=_amount(action.amount, ob.currency) if action.amount else None,
            )
        case p.PRecordTopup():
            record = _ref(by_id, action.obligation_id, "obligation")
            ob = await uow.get(Obligation, record.id)
            return _build(
                c.RecordTopup,
                obligation_id=ob.id,
                amount_minor=_amount(action.amount, ob.currency),
                effective_at=resolve_event_instant(action.on, action.at_time, rc),
                start_from_zero=action.start_from_zero,
            )
        case p.PCorrectBalance():
            record = _ref(by_id, action.obligation_id, "obligation")
            ob = await uow.get(Obligation, record.id)
            return _build(
                c.CorrectBalance,
                obligation_id=ob.id,
                amount_minor=_amount(action.amount, ob.currency),
                effective_at=resolve_event_instant(action.on, action.at_time, rc),
            )
        case p.PChangeDailyRate():
            record = _ref(by_id, action.obligation_id, "obligation")
            ob = await uow.get(Obligation, record.id)
            text = action.rate_per_day.strip()
            sign = -1 if text.startswith("-") else 1
            rate = sign * _amount(text.lstrip("-+"), ob.currency)
            return _build(
                c.ChangeDailyRate,
                obligation_id=ob.id,
                rate_minor_per_day=rate,
                effective_at=resolve_event_instant(action.on, None, rc),
            )
        case p.PSnooze():
            if action.task_id is not None:
                subject_type, record = "task", _ref(by_id, action.task_id, "task")
            else:
                assert action.obligation_id is not None
                subject_type = "obligation"
                record = _ref(by_id, action.obligation_id, "obligation")
            return _build(
                c.SnoozeNotification,
                subject_type=subject_type,
                subject_id=record.id,
                until=resolve_reminder_instant(action.until, rc),
                # A named time is the owner's choice; «позже» may be moved out of quiet hours.
                explicit=isinstance(action.until, When) and action.until.time is not None,
            )
        case p.PSkipOccurrence():
            if action.task_id is not None:
                record = _ref(by_id, action.task_id, "task")
                owner_type = "task"
            else:
                assert action.obligation_id is not None
                record = _ref(by_id, action.obligation_id, "obligation")
                owner_type = "obligation"
            occ = await _occurrence_for(uow, owner_type, record.id, record.title, action.period)
            return c.SkipOccurrence(occurrence_id=occ.id, expected_version=occ.version)
        case p.PArchiveObligation():
            record = _ref(by_id, action.obligation_id, "obligation")
            return c.ArchiveObligation(
                obligation_id=record.id,
                expected_version=record.version,
                outstanding=action.outstanding,
            )
        case p.PUndo():
            return c.UndoCommand()
    raise ValidationFailed("Неизвестное действие", code="unknown_action")
