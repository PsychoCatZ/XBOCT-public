"""OpenAI Responses adapter: the real SDK against a mocked HTTP endpoint.

These tests prove the API contract (request shape, parsing, failures, retries, limits).
They say nothing about the quality of a real model: that is a separate live eval.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from tests.conftest import at
from xboct.application import commands as c
from xboct.application.intents import DAILY_LIMIT_TEXT, FAILURE_TEXT, IntentService
from xboct.llm.contract import CandidateRecord, IntentEnvelope, IntentRequest
from xboct.llm.openai_provider import PROMPT_VERSION, TOOL_NAME, OpenAIIntentProvider
from xboct.llm.provider import IntentProviderError
from xboct.llm.schema import check_strict_subset, strict_schema
from xboct.storage.models import LlmCall, Task

MODEL = "test-model"


class MockOpenAI:
    """Scripted HTTP answers for POST /v1/responses; records every request body."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx2.Response(status, json=body)

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key="sk-test-not-real",
            base_url="https://mock.invalid/v1",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )

    def provider(self) -> OpenAIIntentProvider:
        return OpenAIIntentProvider(model=MODEL, client=self.client())


def tool_answer(
    arguments: dict[str, Any] | str,
    *,
    status: str = "completed",
    extra_output: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": TOOL_NAME,
            "arguments": args,
            "status": "completed",
        }
    ]
    return 200, response_body(output + (extra_output or []), status=status)


def response_body(
    output: list[dict[str, Any]], *, status: str = "completed", incomplete: str | None = None
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1760000000,
        "model": MODEL,
        "status": status,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "usage": {
            "input_tokens": 812,
            "output_tokens": 64,
            "total_tokens": 876,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def status_error(code: int, error_code: str | None = None) -> tuple[int, dict[str, Any]]:
    return code, {"error": {"message": "x", "type": "error", "code": error_code, "param": None}}


CREATE_YULIA = {
    "kind": "actions",
    "actions": [
        {
            "type": "create_task",
            "title": "Ответить Юле",
            "note": None,
            "url": None,
            "next_action": None,
            "recurrence": None,
            "due": {"date": {"kind": "within_week"}, "time": None},
        }
    ],
    "query": None,
    "clarification": None,
    "requires_confirmation": False,
}


def request(message: str = "ответить Юле в течение недели") -> IntentRequest:
    return IntentRequest(
        message=message,
        received_at=at(2026, 10, 8, 10),
        timezone="Asia/Krasnoyarsk",
        candidates=[
            CandidateRecord(id="a" * 32, kind="task", title="Счёт", status="open", version=3)
        ],
    )


def test_tool_schema_fits_strict_subset():
    schema = strict_schema(IntentEnvelope)
    assert check_strict_subset(schema) == []
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


async def test_request_shape():
    mock = MockOpenAI(tool_answer(CREATE_YULIA))
    envelope, meta = await mock.provider().propose_with_meta(request())
    assert envelope.actions[0].type == "create_task"
    [body] = mock.requests
    assert body["model"] == MODEL
    assert body["store"] is False
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == {"type": "function", "name": TOOL_NAME}
    assert body["max_output_tokens"] == 1500
    [tool] = body["tools"]  # one function tool, no built-in web/file/shell tools
    assert tool["type"] == "function"
    assert tool["strict"] is True
    assert check_strict_subset(tool["parameters"]) == []
    assert "previous_response_id" not in body  # context is local, no provider thread
    facts = json.loads(body["input"][-1]["content"])
    assert facts["message"] == "ответить Юле в течение недели"
    assert facts["received_at_local"] == "2026-10-08T10:00+07:00"
    assert facts["weekday"] == "четверг"
    assert facts["candidates"][0]["id"] == "a" * 32
    assert "note" not in facts["candidates"][0]
    assert "DATA" in body["instructions"]
    assert mock.headers[0]["authorization"] == "Bearer sk-test-not-real"
    assert (meta.model, meta.prompt_version, meta.attempts) == (MODEL, PROMPT_VERSION, 1)
    assert (meta.input_tokens, meta.output_tokens) == (812, 64)


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (
            (
                200,
                response_body(
                    [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "refusal", "refusal": "no"}],
                        }
                    ]
                ),
            ),
            "refusal",
        ),
        (
            (200, response_body([], status="incomplete", incomplete="max_output_tokens")),
            "incomplete",
        ),
        (
            (
                200,
                response_body(
                    [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "сделал!", "annotations": []}
                            ],
                        }
                    ]
                ),
            ),
            "invalid",
        ),
        (tool_answer("{not json"), "invalid"),
        (
            tool_answer(
                {
                    "kind": "actions",
                    "actions": [],
                    "query": None,
                    "clarification": None,
                    "requires_confirmation": False,
                }
            ),
            "invalid",
        ),
        (tool_answer({**CREATE_YULIA, "sql": "DROP TABLE task"}), "invalid"),
        (
            tool_answer(
                CREATE_YULIA,
                extra_output=[
                    {
                        "type": "function_call",
                        "id": "fc_2",
                        "call_id": "c2",
                        "name": TOOL_NAME,
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
            ),
            "invalid",
        ),
    ],
)
async def test_unusable_answers_are_controlled_errors(answer, kind):
    mock = MockOpenAI(answer)
    with pytest.raises(IntentProviderError) as exc:
        await mock.provider().propose(request())
    assert exc.value.kind == kind
    assert len(mock.requests) == 1  # no retry for a bad answer


async def test_one_retry_on_transient_failure_only():
    mock = MockOpenAI(status_error(500), tool_answer(CREATE_YULIA))
    envelope, meta = await mock.provider().propose_with_meta(request())
    assert envelope.kind == "actions"
    assert meta.attempts == 2

    mock = MockOpenAI(status_error(503), status_error(502), tool_answer(CREATE_YULIA))
    with pytest.raises(IntentProviderError) as exc:
        await mock.provider().propose(request())
    assert exc.value.kind == "unavailable"
    assert len(mock.requests) == 2  # at most one retry in total (SDK retries are off)

    mock = MockOpenAI(httpx2.ReadTimeout("slow"), httpx2.ReadTimeout("slow"))
    with pytest.raises(IntentProviderError) as exc:
        await mock.provider().propose(request())
    assert exc.value.kind == "timeout"
    assert len(mock.requests) == 2

    mock = MockOpenAI(status_error(429, "rate_limit_exceeded"), tool_answer(CREATE_YULIA))
    assert (await mock.provider().propose(request())).kind == "actions"


@pytest.mark.parametrize(
    "answer",
    [
        status_error(401),
        status_error(403),
        status_error(400),
        status_error(429, "insufficient_quota"),
    ],
)
async def test_no_retry_on_permanent_failure(answer):
    mock = MockOpenAI(answer, tool_answer(CREATE_YULIA))
    with pytest.raises(IntentProviderError) as exc:
        await mock.provider().propose(request())
    assert exc.value.kind == "unavailable"
    assert len(mock.requests) == 1


def test_provider_needs_a_key_or_client():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIIntentProvider(model=MODEL)


async def test_end_to_end_with_database(x):
    mock = MockOpenAI(tool_answer(CREATE_YULIA))
    service = IntentService(x.app, mock.provider())
    result = await service.handle_message("ответить Юле в течение недели", x.ctx())
    assert result.status == "applied"
    assert "15 октября" in result.message  # the date was resolved by code, not the model
    [task] = await x.all(Task)
    assert task.due_date == date(2026, 10, 15)
    [call] = await x.all(LlmCall)
    assert (call.status, call.model, call.prompt_version) == ("ok", MODEL, PROMPT_VERSION)
    assert (call.input_tokens, call.output_tokens) == (812, 64)
    columns = [getattr(call, col.key) for col in LlmCall.__table__.columns]
    assert not any(isinstance(v, str) and "Юле" in v for v in columns)  # no message text


async def test_failure_is_logged_and_never_success(x):
    mock = MockOpenAI(status_error(500), status_error(500))
    service = IntentService(x.app, mock.provider())
    result = await service.handle_message("что-то", x.ctx())
    assert (result.status, result.code, result.message) == (
        "rejected",
        "llm_unavailable",
        FAILURE_TEXT,
    )
    [call] = await x.all(LlmCall)
    assert (call.status, call.error_kind, call.attempts) == ("error", "unavailable", 2)
    assert await x.all(Task) == []


async def test_daily_call_limit_counts_attempts(x):
    mock = MockOpenAI(status_error(500), tool_answer(CREATE_YULIA), tool_answer(CREATE_YULIA))
    service = IntentService(x.app, mock.provider(), daily_call_limit=2)
    first = await service.handle_message("ответить Юле в течение недели", x.ctx())
    assert first.status == "applied"  # used 2 attempts
    second = await service.handle_message("ответить Юле в течение недели", x.ctx())
    assert (second.code, second.message) == ("llm_daily_limit", DAILY_LIMIT_TEXT)
    assert len(mock.requests) == 2  # no call beyond the cap
    assert (await x.run(c.CreateTask(title="Форма работает"))).status == "applied"
    x.clock.advance(days=1)
    third = await service.handle_message("ответить Юле в течение недели", x.ctx())
    assert third.status == "applied"


async def test_reasoning_effort_is_sent_only_when_configured():
    mock = MockOpenAI(tool_answer(CREATE_YULIA), tool_answer(CREATE_YULIA))
    client = mock.client()
    await OpenAIIntentProvider(model=MODEL, client=client, reasoning_effort="none").propose(
        request()
    )
    await OpenAIIntentProvider(model=MODEL, client=client).propose(request())
    assert mock.requests[0]["reasoning"] == {"effort": "none"}
    assert "reasoning" not in mock.requests[1]
    with pytest.raises(ValueError, match="reasoning effort"):
        OpenAIIntentProvider(model=MODEL, client=client, reasoning_effort="extreme")
