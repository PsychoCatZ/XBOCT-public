"""A graceful stop must fit into the container's stop_grace_period.

Nothing here is made to pass by lengthening a timeout: the limits are real (a model
attempt is bounded as a whole), a stop request starts no new update or message, and the
compose grace period is derived from the same constants.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI

from tests.integration.rt import OWNER
from tests.integration.test_openai_contract import request
from xboct import limits
from xboct.application.service import Application
from xboct.llm.openai_provider import ATTEMPT_TIMEOUT_SECONDS, MAX_ATTEMPTS, OpenAIIntentProvider
from xboct.llm.provider import IntentProviderError
from xboct.scheduling.runtime import Runtime
from xboct.storage.models import OutboxMessage, TelegramInbox


def compose_grace_seconds() -> float:
    text = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text()
    match = re.search(r"^\s+stop_grace_period:\s*(\d+)s\s*$", text, re.MULTILINE)
    assert match, "compose.yaml needs stop_grace_period in seconds"
    return float(match.group(1))


def test_compose_grace_period_covers_the_worst_graceful_stop():
    assert limits.MODEL_SECONDS == ATTEMPT_TIMEOUT_SECONDS * MAX_ATTEMPTS == 40
    needed = limits.STOP_BUDGET_SECONDS + limits.GRACE_MARGIN_SECONDS
    assert compose_grace_seconds() >= needed, (
        f"stop_grace_period {compose_grace_seconds()}s < model {limits.MODEL_SECONDS}s + "
        f"telegram {limits.TELEGRAM_SECONDS}s + database {limits.DATABASE_SECONDS}s + margin"
    )


async def test_a_model_attempt_is_bounded_as_a_whole():
    """httpx times each phase on its own; the attempt as a whole must still end in time."""
    gate = asyncio.Event()  # never set: the server accepts and then says nothing

    async def silent(request_: httpx2.Request) -> httpx2.Response:
        await gate.wait()
        return httpx2.Response(200, json={})

    client = AsyncOpenAI(
        api_key="sk-test-not-real",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(silent)),
    )
    provider = OpenAIIntentProvider(model="m", client=client, attempt_timeout=0.05)
    started = asyncio.get_running_loop().time()
    with pytest.raises(IntentProviderError) as exc:
        await provider.propose(request())
    elapsed = asyncio.get_running_loop().time() - started
    assert exc.value.kind == "timeout"
    assert exc.value.meta.attempts == MAX_ATTEMPTS  # one retry, then the controlled error
    assert elapsed < 0.05 * MAX_ATTEMPTS + 1  # two bounded attempts, nothing more


async def test_stop_finishes_the_update_in_flight_and_starts_no_other(bot):
    """The message in flight is handled and its reply saved; nothing else is started."""
    await bot.stop()
    for text in ("первое", "второе"):
        bot.provider.add(text, {"kind": "query", "query": {"kind": "agenda"}})
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.provider.propose

    async def gated(req):
        entered.set()
        await release.wait()  # the language model is still thinking
        return await original(req)

    bot.provider.propose = gated
    bot.say("первое")
    bot.say("второе")
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    runtime.request_stop()  # the stop arrives while the first update waits for the model
    await asyncio.sleep(0)
    assert runtime.lock.held  # not released while work is still running
    release.set()
    await task
    await app.close()
    assert runtime.lock.held is False

    bot.provider.propose = original
    await bot.start()  # a fresh runtime (starting does not process anything yet)
    rows = {r.update_id: r.status for r in await bot.rows(TelegramInbox)}
    assert sorted(rows.values()) == ["done", "received"]  # one finished, one untouched
    # The finished one is saved and answered in the outbox, but no delivery was started
    # after the stop request: it goes out after the restart.
    assert bot.sent == []
    assert [r.status for r in await bot.rows(OutboxMessage)] == ["pending"]

    await bot.tick()  # after the restart: the saved reply goes out, the other is handled
    assert sorted(r.status for r in await bot.rows(TelegramInbox)) == ["done", "done"]
    assert bot.sent == ["На неделе спокойно."] * 2


async def test_stop_sends_no_further_messages(bot):
    await bot.stop()
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    await runtime.start()
    runtime.request_stop()
    from xboct.scheduling.outbox import enqueue

    async with app.sessions() as session, session.begin():
        for i in range(3):
            await enqueue(
                session,
                kind="reply",
                chat_id=OWNER,
                text=f"m{i}",
                dedup_key=f"stop:{i}",
                now=bot.clock.now(),
                local_day=bot.clock.now().astimezone(bot.clock.now().tzinfo).date(),
            )
    assert await runtime.sender.send_due(stop=runtime._stop) == 0
    assert bot.sent == []
    assert await runtime.sender.send_due() == 3  # nothing was lost, only deferred
    await runtime.stop()
    await app.close()
    await bot.start()


# --- review of PR #11: after a stop request no delivery is started ---------------------


async def _enqueue(app, bot, text: str, *, key: str, age_hours: int = 0) -> None:
    from datetime import timedelta

    from xboct.scheduling.outbox import enqueue

    at = bot.clock.now() - timedelta(hours=age_hours)
    async with app.sessions() as session, session.begin():
        await enqueue(
            session,
            kind="reply",
            chat_id=OWNER,
            text=text,
            dedup_key=key,
            now=at,
            local_day=at.date(),
        )


async def _statuses(bot) -> list[tuple[str, str]]:
    rows = await bot.rows(OutboxMessage)
    return sorted((r.text, r.status) for r in rows)


async def test_old_notification_does_not_jump_ahead_of_the_stop(bot):
    """An older notification waits in the queue while a message is being handled."""
    await bot.stop()
    bot.provider.add("привет", {"kind": "query", "query": {"kind": "agenda"}})
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.provider.propose

    async def gated(req):
        entered.set()
        await release.wait()
        return await original(req)

    bot.provider.propose = gated
    bot.say("привет")
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await entered.wait()
    runtime.request_stop()  # the stop is signalled while the message is being handled ...
    # ... and an older notification is in the queue by the time its reply is saved. (Set
    # after the stop on purpose: the scheduler tick may not have run yet and would send
    # it before the signal, which says nothing about the stop.)
    await _enqueue(app, bot, "старое уведомление", key="old:1", age_hours=2)
    release.set()
    await task
    await app.close()
    assert bot.sent == []  # no delivery was started after the stop request

    bot.provider.propose = original
    await bot.start()
    assert await _statuses(bot) == [
        ("На неделе спокойно.", "pending"),  # saved, delivered after the restart
        ("старое уведомление", "pending"),
    ]
    await bot.tick()
    assert sorted(bot.sent) == ["На неделе спокойно.", "старое уведомление"]
    assert all(status == "sent" for _, status in await _statuses(bot))


async def test_a_batch_handled_before_the_stop_is_not_sent_after_it(bot, monkeypatch):
    from xboct.scheduling import runtime as runtime_module

    await bot.stop()
    for i in range(4):
        bot.provider.add(f"сообщение {i}", {"kind": "query", "query": {"kind": "agenda"}})
        bot.say(f"сообщение {i}")
    handled, release = asyncio.Event(), asyncio.Event()
    original = runtime_module.process_pending

    async def pause_after_handling(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result == 4:
            handled.set()  # all four were handled; the send step comes next
            await release.wait()
        return result

    monkeypatch.setattr(runtime_module, "process_pending", pause_after_handling)
    app = await Application.open(bot.db_url, bot.clock)
    # Store the four updates first: the poller then finds nothing new, and exactly one
    # worker (the scheduler tick) handles all four, so the order of events is fixed.
    from xboct.telegram.inbox import poll_once

    assert await poll_once(app.sessions, bot.transport, bot.clock.now, OWNER) == 4
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await handled.wait()
    runtime.request_stop()
    release.set()
    await task
    await app.close()
    assert bot.sent == []  # not four sends after the stop request

    monkeypatch.undo()
    await bot.start()
    assert await _statuses(bot) == [("На неделе спокойно.", "pending")] * 4
    await bot.tick()
    assert bot.sent == ["На неделе спокойно."] * 4  # each exactly once, after the restart


async def test_stop_during_the_first_send_finishes_it_and_starts_no_other(bot):
    await bot.stop()
    first_in_flight, release = asyncio.Event(), asyncio.Event()
    calls = []
    real_send = bot.transport.send_message

    async def gated_send(chat_id, text, buttons):
        calls.append(text)
        if len(calls) == 1:
            first_in_flight.set()
            await release.wait()
        return await real_send(chat_id, text, buttons)

    bot.transport.send_message = gated_send
    app = await Application.open(bot.db_url, bot.clock)
    for i in range(3):
        await _enqueue(app, bot, f"м{i}", key=f"burst:{i}", age_hours=3 - i)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await first_in_flight.wait()
    runtime.request_stop()  # the stop arrives during the first send
    release.set()
    await task
    await app.close()
    assert calls == ["м0"]  # the one in flight finished; no other send was started
    assert bot.sent == ["м0"]

    bot.transport.send_message = real_send
    await bot.start()
    assert await _statuses(bot) == [("м0", "sent"), ("м1", "pending"), ("м2", "pending")]
    await bot.tick()
    assert bot.sent == ["м0", "м1", "м2"]  # nothing lost, nothing sent twice


async def test_a_claim_in_progress_when_the_stop_arrives_sends_nothing_that_came_later(
    bot, monkeypatch
):
    """CI failure of the old-notification test, made deterministic: the scheduler tick is
    already inside send_due (it passed the stop check) when the stop arrives and an older
    notification lands in the queue. The claim itself must see the stop."""
    from xboct.scheduling.outbox import OutboxSender

    await bot.stop()
    in_claim, go = asyncio.Event(), asyncio.Event()
    original_claim = OutboxSender._claim

    async def paused_claim(self, *args):
        in_claim.set()  # send_due passed its stop check and is about to claim
        await go.wait()
        return await original_claim(self, *args)

    monkeypatch.setattr(OutboxSender, "_claim", paused_claim)
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    task = asyncio.create_task(runtime.run())
    await in_claim.wait()
    runtime.request_stop()
    await _enqueue(app, bot, "пришло после сигнала", key="late:1", age_hours=1)
    go.set()
    await task
    await app.close()
    assert bot.sent == []  # nothing was started after the stop request

    monkeypatch.undo()
    await bot.start()
    assert await _statuses(bot) == [("пришло после сигнала", "pending")]
    await bot.tick()
    assert bot.sent == ["пришло после сигнала"]


async def test_a_claim_waiting_for_the_write_lock_sees_a_stop_that_arrives_meanwhile(bot):
    """Second CI failure of the old-notification test (3.13): the claim's transaction begins
    lazily, so a stop check placed before the first query ran before the lock and the
    snapshot. Here the claim waits for the write lock while a message is committed and the
    stop is set; once it gets the lock it must see the stop and claim nothing."""
    from datetime import timedelta

    from xboct.scheduling.outbox import enqueue

    await bot.stop()
    app = await Application.open(bot.db_url, bot.clock)
    runtime = Runtime(app, bot.transport, bot.provider, OWNER, lock_path=bot.lock, poll_timeout=0)
    stop = asyncio.Event()
    at = bot.clock.now() - timedelta(hours=1)
    async with app.sessions() as session, session.begin():
        await enqueue(
            session,
            kind="reply",
            chat_id=OWNER,
            text="пришло после сигнала",
            dedup_key="late:2",
            now=at,
            local_day=at.date(),
        )
        claim = asyncio.create_task(runtime.sender._claim(stop))
        await asyncio.sleep(0.3)  # the claim is now waiting for the write lock
        assert not claim.done()
        stop.set()
    assert await claim is None
    await app.close()
    assert bot.sent == []
