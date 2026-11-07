"""Eval diagnostics and prompt/contract consistency (live eval 583e757: 19/21).

The two failures of the live run left no trace of what the model proposed. These tests
reproduce the suspected mechanism through the real SDK against a mock endpoint and
check that the report now shows the proposal outline, the clarification question or the
validation errors — and nothing secret.
"""

from __future__ import annotations

import json

import pytest

from tests.integration.test_evals import answer, case
from tests.integration.test_openai_contract import MODEL, MockOpenAI
from xboct.cli import _diagnostic_lines, main
from xboct.llm import openai_provider
from xboct.llm.contract import IntentEnvelope, QuerySpec
from xboct.llm.evals import load_corpus, run_corpus
from xboct.llm.openai_provider import PROMPT_VERSION, OpenAIIntentProvider
from xboct.llm.schema import strip_nulls

NO_FILTERS = {"month": None, "year": None, "start_date": None, "end_date": None}


def envelope(**fields) -> dict:
    return {
        "kind": "actions",
        "actions": [],
        "query": None,
        "clarification": None,
        "requires_confirmation": False,
        "conversation_kind": None,
        **fields,
    }


def query(kind: str, period: str = "all", status: str = "all") -> dict:
    return envelope(
        kind="query",
        query={"kind": kind, "period": period, "payment_status": status, **NO_FILTERS},
    )


async def run(case_id: str, *answers):
    mock = MockOpenAI(*answers)
    provider = OpenAIIntentProvider(model=MODEL, client=mock.client(), reasoning_effort="none")
    report = await run_corpus(
        [case(case_id)],
        provider=provider,
        model=MODEL,
        reasoning_effort="none",
        prompt_version=PROMPT_VERSION,
    )
    return report, mock


async def test_agenda_with_a_period_is_refused_and_the_report_says_why():
    # The suspected cause of the failed «что у меня на неделе?»: the prompt told the model
    # to keep this_week for payments, and the strict schema makes the field mandatory.
    report, _ = await run("query-week", answer(query("agenda", period="this_week")))
    [result] = report.results
    assert not result.passed
    assert (result.status, result.code) == ("rejected", "llm_invalid")
    d = result.diagnostics
    assert d["provider_error"]["kind"] == "invalid"
    assert d["proposal"]["kind"] == "query"
    assert d["proposal"]["query"] == {
        "kind": "agenda",
        "period": "this_week",
        "payment_status": "all",
    }
    assert d["validation_errors"] == [
        {
            "where": "query",
            "type": "value_error",
            "message": "period/status filters currently belong to payment queries",
        }
    ]
    lines = _diagnostic_lines(d)
    assert any(line.startswith("proposal:") and "this_week" in line for line in lines)
    assert any("belong to payment queries" in line for line in lines)


async def test_clarification_instead_of_a_preview_shows_the_question_and_candidates():
    # What the live run did not record for A14: the question and who it was about.
    def clarification(candidate_ids: list[str]) -> dict:
        return envelope(
            kind="clarification",
            clarification={"question": "Какой из VPS ты оплатил?", "candidate_ids": candidate_ids},
        )

    spec = case("A14-all-vps")
    ids = {}
    # candidate ids are generated at run time: use a provider that reads them from the request
    mock = MockOpenAI()

    class Echo(OpenAIIntentProvider):
        async def propose_with_meta(self, request):
            ids.update({c.title: c.id for c in request.candidates})
            mock.answers.append(answer(clarification(list(ids.values()))))
            return await super().propose_with_meta(request)

    provider = Echo(model=MODEL, client=mock.client(), reasoning_effort="none")
    report = await run_corpus([spec], provider=provider, model=MODEL, reasoning_effort="none")
    [result] = report.results
    assert not result.passed
    assert result.status == "clarification"
    proposal = result.diagnostics["proposal"]
    assert proposal["kind"] == "clarification"
    assert proposal["clarification"]["question"] == "Какой из VPS ты оплатил?"
    # Candidate ids are shown as the titles they stand for.
    assert sorted(proposal["clarification"]["candidate_ids"]) == ["VPS Германия", "VPS Нидерланды"]
    assert any("expected" in f or "status" in f for f in result.failures)


async def test_report_has_no_secrets_and_no_raw_model_output():
    note = "СЕКРЕТНАЯ ЗАМЕТКА 4242"
    bad = query("agenda", period="this_week")
    bad["actions"] = []
    bad["clarification"] = None
    report, mock = await run("query-week", answer({**bad, "note_leak": note}))
    text = report.to_json()
    assert "sk-test-not-real" not in text
    assert "authorization" not in text.lower()
    assert note not in text  # raw arguments are never copied into the report
    assert json.loads(text)["results"][0]["diagnostics"]["provider_error"]["kind"] == "invalid"
    assert mock.headers[0]["authorization"].startswith("Bearer ")  # it was sent, not reported


async def test_invalid_json_and_wrong_tool_calls_are_described_without_their_content():
    mock = MockOpenAI(
        (
            200,
            {
                **answer(query("agenda"))[1],
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc",
                        "call_id": "c",
                        "name": openai_provider.TOOL_NAME,
                        "arguments": "{not json SECRET",
                        "status": "completed",
                    }
                ],
            },
        )
    )
    provider = OpenAIIntentProvider(model=MODEL, client=mock.client())
    report = await run_corpus([case("query-week")], provider=provider, model=MODEL)
    d = report.results[0].diagnostics
    assert d["validation_errors"][0]["type"] == "json_invalid"
    assert "SECRET" not in report.to_json()


def test_a_successful_proposal_is_outlined_too():
    # Every case carries the proposal, passed or not (next run: compare like with like).
    import asyncio

    report = asyncio.run(run_corpus([case("query-week")]))
    d = report.results[0].diagnostics
    assert d["proposal"]["kind"] == "query"
    assert d["proposal"]["query"]["kind"] == "agenda"
    assert "validation_errors" not in d


def test_eval_only_runs_a_subset_and_rejects_unknown_ids(capsys, tmp_path):
    assert main(["eval", "--only", "query-week,A14-all-vps"]) == 0
    out = capsys.readouterr().out
    assert "fixture: 2/2 passed" in out
    assert main(["eval", "--only", "no-such-case"]) == 2
    assert "unknown case ids: no-such-case" in capsys.readouterr().err
    assert [c["id"] for c in load_corpus(only=["query-week"])] == ["query-week"]


# --- prompt <-> contract ---------------------------------------------------------------


def test_prompt_documents_every_filter_value_the_schema_offers():
    schema = QuerySpec.model_json_schema()["properties"]
    offered = set(schema["period"]["enum"]) | set(schema["payment_status"]["enum"])
    missing = sorted(v for v in offered if v not in openai_provider.INSTRUCTIONS)
    assert missing == []


def test_prompt_keeps_agenda_and_balance_unfiltered_like_the_contract_demands():
    text = openai_provider.INSTRUCTIONS
    assert "query.kind=agenda" in text
    assert "ALWAYS all" in text
    assert "never put this_week, today or any other period on an agenda or balance query" in text
    # The contract the sentence describes:
    valid = IntentEnvelope.model_validate(strip_nulls(query("agenda")))
    assert valid.query is not None
    assert valid.query.period == "all"
    for kind in ("agenda", "balance"):
        with pytest.raises(ValueError, match="belong to payment queries"):
            IntentEnvelope.model_validate(strip_nulls(query(kind, period="this_week")))
        with pytest.raises(ValueError, match="belong to payment queries"):
            IntentEnvelope.model_validate(strip_nulls(query(kind, status="unpaid")))
    IntentEnvelope.model_validate(strip_nulls(query("payments", period="this_week")))


def test_prompt_treats_all_and_both_as_a_decision():
    text = openai_provider.INSTRUCTIONS
    assert "«все X», «оба X», «каждый X»" in text
    assert "one action per fitting candidate" in text


def test_prompt_version_names_this_prompt():
    assert PROMPT_VERSION == "intent-v8-paid-period-rules"


def test_corpus_checks_the_understood_intent_of_the_failed_cases():
    corpus = {c["id"]: c for c in load_corpus()}
    assert corpus["query-week"]["expect"]["intent"]["query.period"] == "all"
    assert corpus["A14-all-vps"]["expect"]["status"] == "preview"  # not weakened
    assert corpus["A14-all-vps"]["expect"]["intent"] == {"kind": "actions"}
    assert {"agenda-today", "both-vps"} <= set(corpus)
