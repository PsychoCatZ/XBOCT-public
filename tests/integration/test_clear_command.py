"""A clear command is applied, not confirmed (live eval ce0490c: A04-tomorrow, 22/23).

The live run answered «завтра оплатить штраф» with a clarification «Создать напоминание
„Оплатить штраф“ на завтра?» and no candidates: an unnecessary confirmation of an
unambiguous command, the task was not created. A prompt cannot be tested without a model;
what is checked here is the rule itself, the consequence in the pipeline (a
confirmation-style clarification changes nothing, never turns into an action) and that
the evaluator, and the next live run's diagnostics, catch the regression.
"""

from __future__ import annotations

import pytest

from tests.integration.test_eval_diagnostics import envelope
from tests.integration.test_evals import answer, case
from tests.integration.test_openai_contract import MODEL, MockOpenAI
from xboct.llm import openai_provider
from xboct.llm.evals import load_corpus, run_corpus
from xboct.llm.openai_provider import PROMPT_VERSION, OpenAIIntentProvider

CONFIRMATION = {
    "question": "Создать напоминание «Оплатить штраф» на завтра?",
    "candidate_ids": [],
}


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
    return report.results[0]


async def test_a_confirmation_of_a_clear_command_creates_nothing_and_is_reported():
    result = await run(
        "A04-tomorrow", answer(envelope(kind="clarification", clarification=CONFIRMATION))
    )
    assert not result.passed
    assert result.status == "clarification"  # the application never acts on it
    assert any("status clarification not in ['applied']" in f for f in result.failures)
    assert any("new tasks 0 != 1" in f for f in result.failures)
    # The next live run's report shows the question that should not have been asked.
    d = result.diagnostics
    assert d["proposal"]["kind"] == "clarification"
    assert d["proposal"]["clarification"] == {
        "question": CONFIRMATION["question"],
        "candidate_ids": [],
    }
    assert d["result"]["status"] == "clarification"


async def test_the_clear_command_itself_is_applied_through_the_real_pipeline():
    task = envelope(
        actions=[
            {
                "type": "create_task",
                "title": "Оплатить штраф",
                "note": None,
                "url": None,
                "next_action": None,
                "recurrence": None,
                "due": {"date": {"kind": "tomorrow"}, "time": None},
            }
        ]
    )
    result = await run("A04-tomorrow", answer(task))
    assert result.passed, result.failures
    assert result.status == "applied"
    assert result.diagnostics["proposal"]["kind"] == "actions"


def test_prompt_states_the_rule_and_keeps_the_legitimate_questions():
    text = " ".join(openai_provider.INSTRUCTIONS.split())
    assert "A clear command" in text
    assert "is applied as it stands: return kind=actions" in text
    assert "Never use kind=clarification to ask «Создать …?»" in text
    # Questions that stay legitimate: a missing or ambiguous piece.
    assert "Ask only when something required is missing or ambiguous" in text
    assert "«на следующей неделе» without a day -> next_week (the application will ask)" in text
    assert "If two or more candidates fit and the text does not decide" in text
    assert PROMPT_VERSION == "intent-v8-paid-period-rules"


def test_the_corpus_keeps_applied_and_checks_the_intent_for_clear_commands():
    corpus = {c["id"]: c for c in load_corpus()}
    a04 = corpus["A04-tomorrow"]["expect"]
    assert a04["status"] == "applied"  # not weakened
    assert a04["intent"] == {"kind": "actions"}
    assert a04["new_tasks"]["due_date"] == "2026-10-09"
    for cid in ("clear-command-day-after", "clear-command-weekday"):
        expect = corpus[cid]["expect"]
        assert expect["status"] == "applied"
        assert expect["intent"] == {"kind": "actions"}


@pytest.mark.parametrize("cid", ["clear-command-day-after", "clear-command-weekday"])
async def test_neighbouring_variants_fail_the_same_way_when_confirmed(cid):
    result = await run(cid, answer(envelope(kind="clarification", clarification=CONFIRMATION)))
    assert not result.passed
    assert result.status == "clarification"
