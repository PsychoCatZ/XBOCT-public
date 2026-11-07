"""Intent eval corpus runner (evals/intent_corpus.json).

Every case runs on a fresh temporary database with a fixed clock, seeded by commands;
one message goes through the real pipeline and the STORED outcome is checked.

Checks are about meaning, not wording: a new task may be titled in any equivalent way
(key words), but dates, amounts, targets, statuses and «nothing else changed» are
strict. A preview is checked by the actions it would apply. A clarification is checked
by its effect (nothing applied), whatever internal code produced it.

- fixture mode: the provider returns the case's `ideal` proposal. This checks the corpus
  and the pipeline (contract), NOT a model.
- live mode: a real provider answers; this costs money and runs only with explicit
  consent. The report records requested/returned model, reasoning effort, prompt
  version, API attempts, usage and latency per case ("unknown" when not reported).
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult
from xboct.application.intents import IntentService
from xboct.application.service import Application
from xboct.domain.clock import FixedClock
from xboct.llm.contract import ContextMessage, IntentEnvelope, IntentRequest
from xboct.llm.openai_provider import outline
from xboct.llm.provider import IntentProvider, IntentProviderError, ProviderMeta
from xboct.storage import migrate
from xboct.storage.db import sqlite_url
from xboct.storage.models import (
    BalanceEvent,
    LlmCall,
    NotificationJob,
    Obligation,
    Occurrence,
    Payment,
    PendingIntent,
    Task,
)
from xboct.style import formal_markers

CORPUS_PATH = Path(__file__).resolve().parents[3] / "evals" / "intent_corpus.json"
DEFAULT_RECEIVED_AT = "2026-10-08T10:00:00+07:00"
UNKNOWN = "unknown"


@dataclass
class CallStats:
    attempts: int | str = UNKNOWN
    input_tokens: int | str = UNKNOWN
    output_tokens: int | str = UNKNOWN
    latency_ms: int | str = UNKNOWN
    returned_model: str = UNKNOWN
    prompt_version: str = UNKNOWN
    error: str | None = None


@dataclass
class CaseResult:
    id: str
    passed: bool
    status: str
    code: str | None
    failures: list[str] = field(default_factory=list)
    call: CallStats = field(default_factory=CallStats)
    # What the model proposed and, if it was refused, why (synthetic corpus only).
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    mode: str
    provider: str
    requested_model: str | None
    reasoning_effort: str | None
    prompt_version: str | None
    results: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    def totals(self) -> dict[str, Any]:
        def total(name: str) -> int | str:
            values = [getattr(r.call, name) for r in self.results]
            known = [v for v in values if isinstance(v, int)]
            if not known:
                return UNKNOWN
            suffix = "" if len(known) == len(values) else f" (+{len(values) - len(known)} unknown)"
            return sum(known) if not suffix else f"{sum(known)}{suffix}"

        return {
            "passed": self.passed,
            "total": len(self.results),
            "api_attempts": total("attempts"),
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "latency_ms": total("latency_ms"),
            "returned_models": sorted({r.call.returned_model for r in self.results}),
        }

    def to_json(self) -> str:
        data = asdict(self)
        data["summary"] = self.totals()
        return json.dumps(data, ensure_ascii=False, indent=2)


def load_corpus(path: Path = CORPUS_PATH, only: list[str] | None = None) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if only:
        known = {c["id"] for c in cases}
        unknown = sorted(set(only) - known)
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(unknown)}")
        cases = [c for c in cases if c["id"] in set(only)]
    return cases


def _resolve_refs(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("@"):
        return ids[value[1:]]
    if isinstance(value, list):
        return [_resolve_refs(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_refs(v, ids) for k, v in value.items()}
    return value


class IdealProvider:
    """Fixture mode: answers with the case's ideal proposal (ids filled in by title)."""

    name = "corpus-ideal"

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.step: int | None = None

    def use_step(self, index: int) -> None:
        """Multi-message cases: each step has its own ideal answer."""
        self.step = index

    @property
    def ideal(self) -> dict[str, Any] | None:
        if self.step is not None:
            return self.case["steps"][self.step].get("ideal")
        return self.case.get("ideal")

    async def propose(self, request: IntentRequest) -> IntentEnvelope:
        return (await self.propose_with_meta(request))[0]

    async def propose_with_meta(
        self, request: IntentRequest
    ) -> tuple[IntentEnvelope, ProviderMeta]:
        if self.ideal is None:  # the case expects the code to answer without a model
            raise IntentProviderError("invalid", "the case has no model answer for this step")
        ids = {cand.title: cand.id for cand in request.candidates}
        envelope = IntentEnvelope.model_validate(_resolve_refs(self.ideal, ids))
        return envelope, ProviderMeta(self.name, "ideal", "corpus")


class RecordingProvider:
    """Passes calls through and keeps the last request, proposal and provider error, so a
    case can check the understood intent (kind, period, status) and the report can show
    what the model proposed or why its answer was refused."""

    def __init__(self, inner: IntentProvider) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "provider")
        self.last: IntentEnvelope | None = None
        self.last_error: IntentProviderError | None = None
        self.request: IntentRequest | None = None
        self.calls = 0

    async def propose(self, request: IntentRequest) -> IntentEnvelope:
        return (await self.propose_with_meta(request))[0]

    async def propose_with_meta(
        self, request: IntentRequest
    ) -> tuple[IntentEnvelope, ProviderMeta]:
        self.calls += 1
        self.request = request
        try:
            if hasattr(self.inner, "propose_with_meta"):
                envelope, meta = await self.inner.propose_with_meta(request)
            else:
                envelope = await self.inner.propose(request)
                meta = ProviderMeta(self.name, None, None)
        except IntentProviderError as exc:
            self.last_error = exc  # the pipeline turns it into «llm_<kind>»
            raise
        self.last = envelope
        return envelope, meta

    def diagnostics(self, result: CommandResult) -> dict[str, Any]:
        """Structure only: intent outline, clarification question, validation errors."""
        titles = {c.id: c.title for c in (self.request.candidates if self.request else [])}

        def readable(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: readable(v) for k, v in node.items()}
            if isinstance(node, list):
                return [readable(v) for v in node]
            return titles.get(node, node) if isinstance(node, str) else node

        out: dict[str, Any] = {"result": {"status": result.status, "code": result.code}}
        out["route"] = "model" if self.calls else "code"  # code: decided with no model call
        if result.status != "answered" or result.code:
            out["result"]["message"] = result.message[:300]  # the application's own text
        if self.last is not None:
            outline_ = outline(self.last.model_dump(mode="json", exclude_none=True))
            out["proposal"] = readable(outline_)
        if self.last_error is not None:
            err = self.last_error
            out["provider_error"] = {"kind": err.kind, "detail": err.detail}
            diagnostic = err.diagnostic or {}
            if "proposal" in diagnostic:
                out["proposal"] = readable(diagnostic["proposal"])
            if diagnostic.get("validation_errors"):
                out["validation_errors"] = diagnostic["validation_errors"]
        return out


def _check_intent(expected: dict[str, Any], envelope: IntentEnvelope | None) -> list[str]:
    """Meaning of the proposal: each expected field may list acceptable values."""
    if envelope is None:
        return ["no proposal was made"]
    actual: dict[str, Any] = {"kind": envelope.kind}
    if envelope.query is not None:
        actual.update({f"query.{k}": v for k, v in envelope.query.model_dump().items()})
    if envelope.conversation_kind is not None:
        actual["conversation_kind"] = envelope.conversation_kind
    if envelope.actions:
        first = envelope.actions[0]
        actual["action"] = first.type
        if first.type == "create_obligation":
            through = first.paid_through
            actual.update(
                {
                    "create.kind": first.kind,
                    "create.currency": first.currency,
                    "create.price_period": first.price_period,
                    "create.paid_through": through.kind if through else None,
                    "create.paid_period": first.paid_period is not None,
                    "create.has_recurrence": first.recurrence is not None,
                    "create.allow_duplicate": first.allow_duplicate,
                }
            )
    failures = []
    for key, allowed in expected.items():
        options = allowed if isinstance(allowed, list) else [allowed]
        if actual.get(key) not in options:
            failures.append(f"intent {key} {actual.get(key)!r} not in {options}")
    return failures


async def run_case(case: dict[str, Any], provider: IntentProvider, workdir: Path) -> CaseResult:
    url = sqlite_url(workdir / f"{case['id']}.db")
    migrate.upgrade(url)
    clock = FixedClock(datetime.fromisoformat(case.get("received_at", DEFAULT_RECEIVED_AT)))
    app = await Application.open(url, clock)
    try:
        created: dict[str, str] = {}  # setup may refer to earlier records as "@Title"
        for i, raw in enumerate(case.get("setup", [])):
            command = c.COMMAND_ADAPTER.validate_python(_resolve_refs(raw, created))
            result = await app.execute(command, CommandContext(f"setup:{i}", "system", clock.now()))
            for key in ("obligation_id", "task_id"):
                if key in result.data and "title" in raw:
                    created[raw["title"]] = result.data[key]
            if result.status != "applied":
                return CaseResult(
                    case["id"],
                    False,
                    result.status,
                    result.code,
                    [f"setup {i} failed: {result.message}"],
                )
        steps = case.get("steps")
        if steps is None:  # one message: the common case, unchanged
            steps = [{"message": case["message"], "expect": case.get("expect", {})}]
        failures: list[str] = []
        dialog: list[ContextMessage] = []
        diagnostics: dict[str, Any] = {}
        step_diagnostics: list[dict[str, Any]] = []
        result: CommandResult | None = None
        for index, step in enumerate(steps):
            if "steps" in case and hasattr(provider, "use_step"):
                provider.use_step(index)
            if step.get("advance_minutes"):
                clock.advance(timedelta(minutes=step["advance_minutes"]))
            for j, raw in enumerate(step.get("between", [])):  # another channel acts meanwhile
                other = c.COMMAND_ADAPTER.validate_python(_resolve_refs(raw, created))
                done = await app.execute(
                    other, CommandContext(f"between:{index}:{j}", "system", clock.now())
                )
                if done.status != "applied":
                    failures.append(f"step {index + 1}: between {j} failed: {done.message}")
            before = await _state(app)
            recorder = RecordingProvider(provider)
            service = IntentService(app, recorder)
            command_id = f"eval:{case['id']}" + (f":{index}" if "steps" in case else "")
            ctx = CommandContext(command_id, "test", clock.now())
            result = await service.handle_message(step["message"], ctx, context=list(dialog))
            expect = step.get("expect", {})
            step_failures = await _check(app, expect, result, before)
            if expect.get("route") == "code" and recorder.calls:
                step_failures.append("the model was called for an answer the code decides")
            if expect.get("route") == "model" and not recorder.calls:
                step_failures.append("the model was not called")
            if "intent" in expect:
                step_failures += _check_intent(expect["intent"], recorder.last)
            if step.get("replay"):  # the same update delivered again: nothing is applied twice
                after_first = await _state(app)
                again = await service.handle_message(step["message"], ctx, context=list(dialog))
                if not again.replayed or again.message != result.message:
                    step_failures.append("a repeated delivery was not answered from the receipt")
                if await _state(app) != after_first:
                    step_failures.append("a repeated delivery changed data")
            if "steps" in case:
                step_failures = [f"step {index + 1}: {f}" for f in step_failures]
                step_diagnostics.append(recorder.diagnostics(result))
            failures += step_failures
            diagnostics = recorder.diagnostics(result)
            now = clock.now()
            dialog.append(ContextMessage(role="user", text=step["message"], at=now))
            dialog.append(ContextMessage(role="assistant", text=result.message, at=now))
            if "steps" in case:
                clock.advance(timedelta(minutes=1))
        assert result is not None
        if "steps" in case:
            diagnostics = {"steps": step_diagnostics}
        return CaseResult(
            case["id"],
            not failures,
            result.status,
            result.code,
            failures,
            await _call_stats(app),
            diagnostics,
        )
    finally:
        await app.close()


async def _call_stats(app: Application) -> CallStats:
    async with app.sessions() as session:
        calls = list(await session.scalars(select(LlmCall).order_by(LlmCall.id)))
    if not calls:
        return CallStats(attempts=0)
    call = calls[-1]

    def known(value: Any) -> Any:
        return UNKNOWN if value is None else value

    def total(name: str) -> Any:
        values = [getattr(c, name) for c in calls]
        if any(v is None for v in values):
            return UNKNOWN
        return sum(values)

    return CallStats(
        attempts=sum(c.attempts for c in calls),
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        latency_ms=total("latency_ms"),
        returned_model=known(call.model),
        prompt_version=known(call.prompt_version),
        error=call.error_kind,
    )


async def _state(app: Application) -> dict[str, Any]:
    """Everything a message could change, in comparable form."""
    async with app.sessions() as session:
        tasks = {
            t.id: (t.title, t.status, t.due_date, t.due_at, t.next_action, t.note)
            for t in await session.scalars(select(Task))
        }
        occurrences = {
            o.id: (o.status, o.effective_due) for o in await session.scalars(select(Occurrence))
        }
        obligations = {
            o.id: (
                o.active,
                o.coverage_until,
                o.title,
                o.kind,
                o.currency,
                o.amount_minor,
                o.note,
                o.recurrence_id,
            )
            for o in await session.scalars(select(Obligation))
        }
        payments = len(list(await session.scalars(select(Payment.id))))
        balance = len(list(await session.scalars(select(BalanceEvent.id))))
    return {
        "tasks": tasks,
        "occurrences": occurrences,
        "obligations": obligations,
        "payments": payments,
        "balance_events": balance,
    }


def _title_matches(title: str, keywords: list[str]) -> bool:
    lowered = title.casefold()
    return any(k.casefold() in lowered for k in keywords)


async def _check(
    app: Application, expect: dict[str, Any], result: CommandResult, before: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    allowed = expect.get("status")
    if allowed is not None:
        allowed_set = {allowed} if isinstance(allowed, str) else set(allowed)
        if result.status not in allowed_set:
            failures.append(f"status {result.status} not in {sorted(allowed_set)} ({result.code})")
    # XBOCT's own wording is «ты» (xboct.style). Checked on every clarification and wherever a
    # case asks for it; text inside «quotes» is the owner's data and is not judged.
    if result.status == "clarification" or expect.get("ty_style"):
        breaches = formal_markers(result.message)
        if breaches:
            failures.append(f"style: not «ты» ({', '.join(sorted(set(breaches)))})")
    if "text_contains" in expect and expect["text_contains"] not in result.message:
        failures.append(f"text lacks {expect['text_contains']!r}")
    folded = expect.get("text_contains_folded")
    if folded is not None and folded.casefold() not in result.message.casefold():
        failures.append(f"text lacks {folded!r} (any case)")
    # Which records the answer is about (titles), not its exact wording.
    for title in expect.get("mentions", []):
        if title not in result.message:
            failures.append(f"answer does not mention {title!r}")
    for title in expect.get("omits", []):
        if title in result.message:
            failures.append(f"answer should not mention {title!r}")
    results = result.data.get("results") or [result.data]
    data = results[0] if results else {}
    for key, value in expect.get("data", {}).items():
        if data.get(key) != value:
            failures.append(f"data.{key} {data.get(key)!r} != {value!r}")

    after = await _state(app)
    if expect.get("no_mutation") and after != before:
        changed = [k for k in after if after[k] != before[k]]
        failures.append(f"data changed although nothing should: {changed}")
    if "payments" in expect and after["payments"] != expect["payments"]:
        failures.append(f"payments {after['payments']} != {expect['payments']}")

    new_ids = set(after["tasks"]) - set(before["tasks"])
    if "new_tasks" in expect:
        spec = expect["new_tasks"]
        if len(new_ids) != spec.get("count", 1):
            failures.append(f"new tasks {len(new_ids)} != {spec.get('count', 1)}")
        for task_id in new_ids:
            title, status, due_date, _due_at, _next, note = after["tasks"][task_id]
            if "title_any" in spec and not _title_matches(title, spec["title_any"]):
                failures.append(f"new task title {title!r} lacks any of {spec['title_any']}")
            if (
                "due_date" in spec
                and (due_date.isoformat() if due_date else None) != spec["due_date"]
            ):
                failures.append(f"new task due {due_date} != {spec['due_date']}")
            if "status" in spec and status != spec["status"]:
                failures.append(f"new task status {status} != {spec['status']}")
            if "note_contains" in spec and spec["note_contains"] not in (note or "") + title:
                failures.append("new task does not keep the quoted text as data")

    for title, fields in expect.get("tasks", {}).items():
        match = [v for k, v in after["tasks"].items() if k not in new_ids and v[0] == title]
        if not match:
            failures.append(f"task {title!r} not found")
            continue
        _t, status, due_date, _due_at, next_action, _note = match[0]
        actual = {
            "status": status,
            "due_date": due_date.isoformat() if due_date else None,
            "next_action_set": bool(next_action),
        }
        for name, value in fields.items():
            if actual.get(name) != value:
                failures.append(f"task {title!r}.{name} {actual.get(name)!r} != {value!r}")

    failures += await _check_obligations(app, expect, before, after)

    async with app.sessions() as session:
        if expect.get("no_pending"):
            live = await session.scalar(
                select(PendingIntent).where(PendingIntent.status == "pending").limit(1)
            )
            if live is not None:
                failures.append("a preview is pending although none should be")
        if "pending_status" in expect:
            latest = await session.scalar(
                select(PendingIntent).order_by(PendingIntent.created_at.desc()).limit(1)
            )
            if latest is None or latest.status != expect["pending_status"]:
                failures.append(
                    f"latest preview is {latest.status if latest else None}, "
                    f"expected {expect['pending_status']}"
                )
        for title, periods in expect.get("periods", {}).items():
            ob = await session.scalar(select(Obligation).where(Obligation.title == title))
            if ob is None:
                failures.append(f"obligation {title!r} not found")
                continue
            rows = await session.scalars(select(Occurrence).where(Occurrence.owner_id == ob.id))
            status = {o.period_key: o.status for o in rows}
            for key, value in periods.items():
                if status.get(key) != value:
                    failures.append(f"{title} {key} {status.get(key)!r} != {value!r}")
        if "pending_actions" in expect:
            failures += await _check_pending(session, expect["pending_actions"])
    return failures


async def _check_obligations(
    app: Application, expect: dict[str, Any], before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    """Obligations created by this message, as stored (the code derives the boundary)."""
    failures: list[str] = []
    new_ids = set(after["obligations"]) - set(before["obligations"])
    if "obligation_count" in expect and len(after["obligations"]) != expect["obligation_count"]:
        failures.append(f"obligations {len(after['obligations'])} != {expect['obligation_count']}")
    spec = expect.get("new_obligations")
    if spec is None:
        return failures
    if len(new_ids) != spec.get("count", 1):
        return [*failures, f"new obligations {len(new_ids)} != {spec.get('count', 1)}"]
    for ob_id in new_ids:
        _active, until, title, kind, currency, amount, note, recurrence = after["obligations"][
            ob_id
        ]
        actual = {
            "title": title,
            "kind": kind,
            "currency": currency,
            "amount_minor": amount,
            "coverage_until": until.isoformat() if until else None,
            "note": note,
            "has_recurrence": recurrence is not None,
        }
        if "title_folded" in spec and title.casefold() != spec["title_folded"].casefold():
            failures.append(f"new obligation title {title!r} is not {spec['title_folded']!r}")
        for name in ("title", "kind", "currency", "amount_minor", "coverage_until", "note"):
            if name in spec and actual[name] != spec[name]:
                failures.append(f"new obligation {name} {actual[name]!r} != {spec[name]!r}")
        if "has_recurrence" in spec and actual["has_recurrence"] != spec["has_recurrence"]:
            failures.append(f"new obligation has_recurrence {actual['has_recurrence']}")
        for part in spec.get("note_contains", []):
            if part not in (note or ""):
                failures.append(f"new obligation note lacks {part!r}")
        if "jobs" in spec:
            async with app.sessions() as session:
                kinds = set(
                    await session.scalars(
                        select(NotificationJob.kind).where(
                            NotificationJob.subject_id == ob_id, NotificationJob.status == "pending"
                        )
                    )
                )
            if not set(spec["jobs"]) <= kinds:
                failures.append(
                    f"reminders {sorted(kinds)} lack {sorted(set(spec['jobs']) - kinds)}"
                )
    return failures


async def _check_pending(session: Any, expected: list[dict[str, Any]]) -> list[str]:
    """Compare the preview's actions with the expected set (order-insensitive)."""
    pending = await session.scalar(
        select(PendingIntent)
        .where(PendingIntent.status == "pending")
        .order_by(PendingIntent.created_at.desc())
        .limit(1)
    )
    if pending is None:
        return ["no pending preview"]
    titles: dict[str, str] = {}
    for model in (Task, Obligation):
        for row in await session.scalars(select(model)):
            titles[row.id] = row.title
    periods = {o.id: o.period_key for o in await session.scalars(select(Occurrence))}
    actual = []
    for action in pending.actions:
        target = action.get("task_id") or action.get("obligation_id") or action.get("subject_id")
        if action["type"] == "create_obligation":
            target = action.get("title")  # nothing exists yet: the preview names it
        name = titles.get(target, target)
        item = {"type": action["type"], "target": name}
        if isinstance(name, str):
            item["target_folded"] = name.casefold()  # a title the model derived: case is free
        for key in ("currency", "coverage_until", "kind"):
            if action.get(key) is not None:
                item[key] = action[key]
        if action.get("occurrences"):
            item["periods"] = sorted(periods[o["id"]] for o in action["occurrences"])
        if action.get("amount_minor") is not None:
            item["amount_minor"] = action["amount_minor"]
        actual.append(item)
    keys = {k for e in expected for k in e}

    def canon(items: list[dict[str, Any]]) -> Counter[str]:
        return Counter(
            json.dumps({k: i.get(k) for k in sorted(keys)}, ensure_ascii=False, sort_keys=True)
            for i in items
        )

    if canon(actual) != canon(expected):
        return [f"preview actions {actual} != expected {expected}"]
    return []


async def run_corpus(
    cases: list[dict[str, Any]],
    *,
    provider: IntentProvider | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    prompt_version: str | None = None,
) -> Report:
    """provider=None -> fixture mode (ideal answers); otherwise live mode."""
    results = []
    with tempfile.TemporaryDirectory(prefix="xboct-eval-") as tmp:
        for case in cases:
            chosen = provider if provider is not None else IdealProvider(case)
            results.append(await run_case(case, chosen, Path(tmp)))
    return Report(
        mode="fixture" if provider is None else "live",
        provider="corpus-ideal" if provider is None else getattr(provider, "name", "?"),
        requested_model=model,
        reasoning_effort=reasoning_effort,
        prompt_version=prompt_version,
        results=results,
    )
