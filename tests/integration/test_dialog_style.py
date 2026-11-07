"""Dialog style: «ты», short and natural; the owner's own words are data and stay as written."""

from __future__ import annotations

import pytest

from tests.integration.test_eval_diagnostics import envelope, run
from tests.integration.test_evals import answer, case
from xboct.application.intents import FAILURE_TEXT
from xboct.application.render import forecast_brief
from xboct.llm import openai_provider
from xboct.llm.evals import load_corpus, run_corpus
from xboct.style import formal_markers


def clarification(question: str) -> dict:
    return answer(
        envelope(kind="clarification", clarification={"question": question, "candidate_ids": []})
    )


def test_the_prompt_states_the_style_rule_and_protects_the_owners_text():
    text = " ".join(openai_provider.INSTRUCTIONS.split())
    assert "to the owner as «ты» — never «вы», «Вы», «вам», «ваш» and never «пожалуйста»" in text
    assert "«Уточни…», «Выбери…», «Укажи…»" in text
    assert "Calling the owner «Кот» is allowed, not in every message" in text
    assert "titles, notes, quotes and any other text of the owner stay exactly as written" in text
    assert "never reworded, corrected or converted to «ты»" in text
    assert openai_provider.PROMPT_VERSION == "intent-v8-paid-period-rules"


def test_the_prompt_keeps_the_earlier_rules():
    text = " ".join(openai_provider.INSTRUCTIONS.split())
    assert "A clear command" in text  # v5
    assert "ALWAYS all" in text  # v4
    assert "«все X», «оба X», «каждый X»" in text


# --- the Demo dialogue: clarification wording is judged by the eval -----------------------


@pytest.mark.parametrize(
    "question",
    [
        "Какой Demo оплатил — подписку или API? Выбери.",
        "Уточни, какой Demo: подписка или API?",
        "Укажи, про какой Demo речь.",
        "«Вы» или «Demo API»? Выбери один.",  # quoted titles are the owner's data
    ],
)
async def test_a_clarification_in_style_passes(question):
    report, _ = await run("demo-which", clarification(question))
    [result] = report.results
    assert result.passed, result.failures
    assert result.status == "clarification"


@pytest.mark.parametrize(
    ("question", "marker"),
    [
        ("Уточните, пожалуйста, какой Demo — подписка или API?", "пожалуйста"),
        ("Вы имеете в виду подписку или API?", "Вы"),
        ("Выберите, какой Demo оплатили.", "Выберите"),
        ("Укажите, какой Demo.", "Укажите"),
        ("Какой из ваших Demo?", "ваших"),
    ],
)
async def test_a_formal_clarification_fails_the_case(question, marker):
    report, _ = await run("demo-which", clarification(question))
    [result] = report.results
    assert not result.passed
    assert result.status == "clarification"  # nothing was applied, the wording is the failure
    assert any(f.startswith("style: not «ты»") and marker in f for f in result.failures)


def test_the_demo_cases_are_in_the_corpus_with_the_style_check():
    corpus = {c["id"]: c for c in load_corpus()}
    for case_id in ("demo-which", "demo-which-top-up"):
        expect = corpus[case_id]["expect"]
        assert expect["status"] == "clarification"
        assert expect["no_mutation"] is True
        assert expect["ty_style"] is True
        assert formal_markers(corpus[case_id]["ideal"]["clarification"]["question"]) == []


async def test_every_clarification_is_judged_even_without_the_flag():
    # A09 asks about «до января»; the app's own question is in style.
    report = await run_corpus([case("A09-until-january")])  # the fixture's ideal proposal
    assert report.results[0].passed, report.results[0].failures


# --- the owner's own words are not rewritten ----------------------------------------------------


async def test_the_owners_own_vy_and_pozhaluysta_stay_in_the_title():
    corpus = {c["id"]: c for c in load_corpus()}
    case = corpus["own-words-stay-as-written"]
    assert "вы" in case["message"]
    assert "пожалуйста" in case["message"]
    assert case["expect"]["new_tasks"]["title_any"] == ["как вы там с Demo, пожалуйста"]
    report, _ = await run(
        "own-words-stay-as-written",
        answer(
            envelope(
                actions=[
                    {
                        "type": "create_task",
                        "title": "Спросить у Кота, как вы там с Demo, пожалуйста",
                    }
                ]
            )
        ),
    )
    [result] = report.results
    assert result.passed, result.failures
    assert result.status == "applied"


# --- fixed phrases -------------------


def test_fixed_phrases_that_used_to_be_formal_are_in_style():
    from xboct.application.render import forecast_text  # noqa: F401  (imported for the scan)

    assert "укажи остаток" in forecast_brief(_no_baseline())
    assert FAILURE_TEXT == "Не разобрал сообщение; данные не менял. Можно сделать через форму."
    for phrase in (forecast_brief(_no_baseline()), FAILURE_TEXT):
        assert formal_markers(phrase) == []


def _no_baseline():
    from xboct.domain.balance import BalanceForecast

    return BalanceForecast(
        as_of=None,  # type: ignore[arg-type]
        status="no_baseline",
        estimate_minor=None,
        rate_minor_per_day=None,
        days_left=None,
        depletion_at=None,
        threshold_at=None,
        threshold_days=5,
        baseline_event_id=None,
        baseline_at=None,
        baseline_assumed=False,
        funding_event_id=None,
        funding_at=None,
    )
