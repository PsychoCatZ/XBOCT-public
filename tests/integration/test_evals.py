"""The eval corpus: fixture mode through the real pipeline, plus the evaluator itself.

Passing in fixture mode proves the corpus and the pipeline agree. It is NOT a model
evaluation; that is the separate, paid live run.
"""

import copy
import json

from tests.integration.test_openai_contract import MODEL, MockOpenAI, response_body
from xboct.cli import main
from xboct.llm.evals import UNKNOWN, load_corpus, run_corpus
from xboct.llm.openai_provider import PROMPT_VERSION, TOOL_NAME, OpenAIIntentProvider


def case(case_id: str) -> dict:
    [found] = [c for c in load_corpus() if c["id"] == case_id]
    return copy.deepcopy(found)


def with_ideal(case_id: str, ideal: dict) -> dict:
    return {**case(case_id), "ideal": ideal}


async def test_corpus_passes_with_ideal_proposals():
    cases = load_corpus()
    assert len(cases) >= 12
    assert len({c["id"] for c in cases}) == len(cases)
    report = await run_corpus(cases)
    assert [(r.id, r.failures) for r in report.results if not r.passed] == []
    assert report.mode == "fixture"
    assert all(r.call.input_tokens == UNKNOWN for r in report.results)  # fixture: no usage


async def test_equivalent_task_title_is_accepted_but_facts_stay_strict():
    reworded = with_ideal(
        "A01-within-week",
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_task",
                    "title": "Написать ответ Юлии",
                    "due": {"date": {"kind": "within_week"}},
                }
            ],
        },
    )
    assert (await run_corpus([reworded])).results[0].passed
    wrong_due = with_ideal(
        "A01-within-week",
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_task",
                    "title": "Ответить Юле",
                    "due": {"date": {"kind": "tomorrow"}},
                }
            ],
        },
    )
    result = (await run_corpus([wrong_due])).results[0]
    assert not result.passed
    assert any("due 2026-10-09 != 2026-10-15" in f for f in result.failures)
    unrelated = with_ideal(
        "A01-within-week",
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_task",
                    "title": "Купить хлеб",
                    "due": {"date": {"kind": "within_week"}},
                }
            ],
        },
    )
    assert not (await run_corpus([unrelated])).results[0].passed


async def test_wrong_meaning_is_caught():
    completed = with_ideal(
        "A02-ready-not-sent",
        {"kind": "actions", "actions": [{"type": "complete_task", "task_id": "@Ответить Юле"}]},
    )
    result = (await run_corpus([completed])).results[0]
    assert not result.passed
    assert any("status 'completed' != 'open'" in f for f in result.failures)


async def test_preview_is_checked_by_its_actions():
    one_vps = with_ideal(
        "A14-all-vps",
        {
            "kind": "actions",
            "requires_confirmation": True,
            "actions": [
                {
                    "type": "mark_payment",
                    "obligation_id": "@VPS Германия",
                    "periods": [{"kind": "current"}],
                }
            ],
        },
    )
    result = (await run_corpus([one_vps])).results[0]
    assert result.status == "preview"
    assert not result.passed
    assert any("preview actions" in f for f in result.failures)
    wrong_amount = with_ideal(
        "mixed-message",
        {
            "kind": "actions",
            "actions": [
                {"type": "record_topup", "obligation_id": "@Германия", "amount": "100"},
                {"type": "complete_task", "task_id": "@Письмо в банк"},
            ],
        },
    )
    assert not (await run_corpus([wrong_amount])).results[0].passed


async def test_clarification_is_judged_by_effect_not_code():
    asked = with_ideal(
        "A09-until-january",
        {
            "kind": "clarification",
            "clarification": {"question": "До какого числа января оплачено?"},
        },
    )
    assert (await run_corpus([asked])).results[0].passed
    asked_week = with_ideal(
        "next-week-needs-day",
        {"kind": "clarification", "clarification": {"question": "В какой день следующей недели?"}},
    )
    assert (await run_corpus([asked_week])).results[0].passed
    guessed = with_ideal(
        "A15-which-vps",
        {
            "kind": "actions",
            "actions": [
                {"type": "record_topup", "obligation_id": "@VPS Германия", "amount": "1000"}
            ],
        },
    )
    result = (await run_corpus([guessed])).results[0]
    assert not result.passed  # guessing a VPS changes data


def answer(arguments: dict, *, usage: bool = True) -> tuple[int, dict]:
    body = response_body(
        [
            {
                "type": "function_call",
                "id": "fc",
                "call_id": "c",
                "name": TOOL_NAME,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "status": "completed",
            }
        ]
    )
    body["model"] = "gpt-6-luna-2026-09-01"
    if not usage:
        body["usage"] = None
    return 200, body


async def test_live_style_report_records_attempts_usage_latency_and_models():
    a01 = case("A01-within-week")
    query = case("query-week")
    mock = MockOpenAI(
        (500, {"error": {"message": "x", "type": "error", "code": None, "param": None}}),
        answer(
            {**a01["ideal"], "query": None, "clarification": None, "requires_confirmation": False}
        ),
        answer(
            {
                "kind": "query",
                "query": {"kind": "agenda"},
                "actions": [],
                "clarification": None,
                "requires_confirmation": False,
            },
            usage=False,
        ),
    )
    provider = OpenAIIntentProvider(model=MODEL, client=mock.client(), reasoning_effort="none")
    report = await run_corpus(
        [a01, query],
        provider=provider,
        model=MODEL,
        reasoning_effort="none",
        prompt_version=PROMPT_VERSION,
    )
    first, second = report.results
    assert first.passed
    assert second.passed
    assert first.call.attempts == 2  # one transient retry
    assert (first.call.input_tokens, first.call.output_tokens) == (812, 64)
    assert first.call.returned_model == "gpt-6-luna-2026-09-01"
    assert first.call.prompt_version == PROMPT_VERSION
    assert isinstance(first.call.latency_ms, int)
    assert (second.call.input_tokens, second.call.output_tokens) == (UNKNOWN, UNKNOWN)
    summary = json.loads(report.to_json())
    assert summary["requested_model"] == MODEL
    assert summary["reasoning_effort"] == "none"
    assert summary["summary"]["api_attempts"] == 3
    assert summary["summary"]["input_tokens"] == "812 (+1 unknown)"
    assert summary["summary"]["returned_models"] == ["gpt-6-luna-2026-09-01"]
    assert all(r["reasoning"] == {"effort": "none"} for r in mock.requests)


def test_eval_cli_fixture_and_live_guard(tmp_path, capsys, monkeypatch):
    out = tmp_path / "report.json"
    assert main(["eval", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["passed"] == report["summary"]["total"]
    assert "attempts=" in capsys.readouterr().out
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    assert main(["eval", "--live"]) == 2  # refuses paid calls without explicit consent
    assert "--i-accept-paid-calls" in capsys.readouterr().err


def payments_query(period: str, status: str) -> dict:
    return {
        "kind": "query",
        "query": {"kind": "payments", "period": period, "payment_status": status},
    }


async def test_payment_period_and_status_are_checked_by_meaning():
    # Dropping the month makes the answer a full overview: wrong intent, extra records.
    overview = with_ideal("pay-this-month-unpaid", payments_query("all", "all"))
    result = (await run_corpus([overview])).results[0]
    assert not result.passed
    assert any("intent query.period 'all'" in f for f in result.failures)
    # History instead of the current paid state.
    history = with_ideal("covered-now", payments_query("all", "paid"))
    result = (await run_corpus([history])).results[0]
    assert any("intent query.payment_status 'paid'" in f for f in result.failures)
    # An equivalent acceptable reading passes (next month, unpaid or all).
    alt = with_ideal("pay-next-month", payments_query("next_month", "all"))
    assert (await run_corpus([alt])).results[0].passed


async def test_social_remark_must_not_repeat_the_list_or_change_data():
    listed = with_ideal("conversation-frustration", payments_query("all", "all"))
    result = (await run_corpus([listed])).results[0]
    assert not result.passed
    assert any("should not mention 'Аренда'" in f for f in result.failures)
    mutating = with_ideal(
        "conversation-thanks",
        {
            "kind": "actions",
            "actions": [{"type": "create_task", "title": "Спасибо", "due": None}],
        },
    )
    result = (await run_corpus([mutating])).results[0]
    assert any("data changed although nothing should" in f for f in result.failures)
