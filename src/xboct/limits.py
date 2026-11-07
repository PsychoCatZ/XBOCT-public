"""Time limits that decide how long a graceful stop can take.

A stop request interrupts only waiting. What may still be running afterwards is one
language-model request (every attempt is bounded as a whole) or one Telegram send that was
already in flight, and the database steps around it. Once a stop is requested no new update
is handled and no new delivery is started: replies that were saved wait in the outbox and
go out after the next start. The sum below is therefore a deliberately generous bound.
The compose `stop_grace_period` must cover the sum below — tests/integration/
test_stop_budget.py fails when it no longer does, so a changed timeout cannot silently
break shutdown.
"""

from __future__ import annotations

from xboct.llm.openai_provider import ATTEMPT_TIMEOUT_SECONDS, MAX_ATTEMPTS
from xboct.storage.db import BUSY_TIMEOUT_MS
from xboct.telegram.aiogram_transport import DEFAULT_REQUEST_TIMEOUT

MODEL_SECONDS = ATTEMPT_TIMEOUT_SECONDS * MAX_ATTEMPTS  # one request: attempt + one retry
TELEGRAM_SECONDS = DEFAULT_REQUEST_TIMEOUT  # the reply being sent when the stop arrives
DATABASE_SECONDS = 2 * BUSY_TIMEOUT_MS / 1000  # write locks waited for around those two
STOP_BUDGET_SECONDS = MODEL_SECONDS + TELEGRAM_SECONDS + DATABASE_SECONDS
GRACE_MARGIN_SECONDS = 10  # process exit, uvicorn shutdown, closing the engine
