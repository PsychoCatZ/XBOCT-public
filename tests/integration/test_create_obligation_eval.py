"""The multi-message eval runner and the prompt rules for creating records.

The Demo chain needs one temporary database and the dialogue passed between steps; a set of
single phrases cannot show it. These tests check that the runner really does that and really
fails on a wrong expectation, so the corpus cases mean something.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.integration.test_evals import case
from xboct.llm import openai_provider
from xboct.llm.contract import IntentEnvelope, PaidPeriod, PaidThrough, PCreateObligation
from xboct.llm.evals import IdealProvider, load_corpus, run_case, run_corpus

STEP_CASES = [
    "demo-create-clear",
    "demo-dialogue-order",
    "demo-create-incomplete",
    "demo-create-no",
    "demo-yes-after-expiry",
    "demo-duplicate",
    "create-currency-unclear",
    "create-no-amount",
    "create-until-january",
    "create-inclusive-unclear",
    "create-year-border",
    "create-calendar-rent",
    "create-calendar-no-date",
    "demo-purchase-period",
    "demo-purchase-price-only",
    "demo-period-conflict",
    "demo-twin-after-preview",
]


@pytest.mark.parametrize("case_id", [*STEP_CASES, "yes-without-preview"])
async def test_the_new_cases_pass_in_fixture_mode(case_id):
    report = await run_corpus([case(case_id)])
    [result] = report.results
    assert result.passed, result.failures


def test_the_corpus_has_every_regression_the_task_asks_for():
    ids = {c["id"] for c in load_corpus()}
    assert set(STEP_CASES) | {"yes-without-preview"} <= ids
    steps = {c["id"]: c for c in load_corpus() if "steps" in c}
    assert all(len(steps[i]["steps"]) >= 1 for i in steps)
    assert len(steps["demo-dialogue-order"]["steps"]) == 3  # the synthetic dialogue, in its order


class Spy(IdealProvider):
    """The ideal answers, remembering what each step was shown as the dialogue so far."""

    def __init__(self, source):
        super().__init__(source)
        self.contexts = []
        self.pending = []

    async def propose_with_meta(self, request):
        self.contexts.append(list(request.context))
        self.pending.append(request.pending_preview)
        return await super().propose_with_meta(request)


async def test_the_dialogue_and_the_saved_preview_reach_the_next_step(tmp_path):
    source = case("demo-dialogue-order")
    spy = Spy(source)
    result = await run_case(source, spy, tmp_path)
    assert result.passed, result.failures
    assert spy.contexts[0] == []
    assert [m.role for m in spy.contexts[1]] == ["user", "assistant"]
    assert spy.contexts[1][0].text == source["steps"][0]["message"]
    assert "Проверь, что применяю" in spy.contexts[1][1].text  # our own preview, as sent
    # The model is told what preview is waiting, so «Да» has a target to answer.
    assert spy.pending[0] is None
    assert spy.pending[1] is not None
    assert "новая подписка «Demo»" in spy.pending[1]
    assert len(spy.pending) == 2  # the closing «Да» is answered by the code, no model call


async def test_a_wrong_expectation_fails_the_case(tmp_path):
    source = copy.deepcopy(case("demo-create-clear"))
    source["steps"][1]["expect"]["new_obligations"]["coverage_until"] = "2026-11-10"  # off by one
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert not result.passed
    assert any("step 2" in f and "coverage_until" in f for f in result.failures)


async def test_a_second_obligation_where_one_is_expected_fails(tmp_path):
    source = copy.deepcopy(case("demo-duplicate"))
    source["steps"][2]["expect"]["obligation_count"] = 1
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert not result.passed
    assert any("obligations 2 != 1" in f for f in result.failures)


async def test_a_formal_clarification_fails_the_style_check(tmp_path):
    source = copy.deepcopy(case("demo-create-incomplete"))
    # The model, not the code, asks — formally. (The code's own question is in style.)
    source["steps"][0]["ideal"] = {
        "kind": "clarification",
        "clarification": {
            "question": "Уточните, пожалуйста, до какого дня оплачено.",
            "candidate_ids": [],
        },
    }
    source["steps"][0]["expect"].pop("text_contains")
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert not result.passed
    assert any("style: not «ты»" in f for f in result.failures)


async def test_a_confirmation_question_without_a_preview_is_caught(tmp_path):
    """The original failure: the model asks «Создать …?» as a clarification. The case wants
    a saved preview, so a clarification instead fails it."""
    source = copy.deepcopy(case("demo-create-clear"))
    source["steps"][0]["ideal"] = {
        "kind": "clarification",
        "clarification": {
            "question": "Создать подписку Demo: 17 долларов ежемесячно?",
            "candidate_ids": [],
        },
    }
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert not result.passed
    assert any("step 1" in f and "status clarification" in f for f in result.failures)
    assert any("step 2" in f for f in result.failures)  # and «Да» then has nothing to confirm


async def test_a_repeated_delivery_is_checked(tmp_path):
    source = copy.deepcopy(case("demo-create-clear"))
    assert source["steps"][1]["replay"] is True
    result = await run_case(source, IdealProvider(source), tmp_path)
    assert result.passed, result.failures


def test_totals_add_up_over_all_steps_of_a_case():
    from xboct.llm.evals import UNKNOWN

    assert UNKNOWN == "unknown"  # token totals are sums over every call, unknown stays unknown


# --- the prompt states the rules ----------------------------------------------


def prompt() -> str:
    return " ".join(openai_provider.INSTRUCTIONS.split())


def test_the_prompt_names_the_new_action_and_its_rules():
    text = prompt()
    assert "is create_obligation with kind=coverage" in text
    assert "kind=calendar with a recurrence whose start date is known" in text
    assert "A VPS with a balance is not created by text" in text
    assert "the application saves a preview itself" in text
    assert "never return a clarification to confirm something that has no saved preview" in text
    assert "Never add or subtract a day yourself" in text
    assert "Do not ask for the first payment date" in text
    assert "An amount without a named currency: currency null" in text
    assert "No amount: amount and currency null — never invent either" in text
    assert "is the PRICE period (price_period); it is not a recurrence" in text
    assert "Never set recurrence for kind=coverage" in text
    assert "do not guess a start date" in text
    assert "only the owner's own note, as written" in text
    assert "allow_duplicate=true only when the owner explicitly asks" in text
    assert openai_provider.PROMPT_VERSION == "intent-v8-paid-period-rules"


def test_the_prompt_covers_every_value_of_the_new_contract():
    fields = PCreateObligation.model_json_schema()["properties"]
    values = set(fields["currency"]["anyOf"][0]["enum"]) | set(fields["kind"]["enum"])
    values |= set(fields["price_period"]["anyOf"][0]["enum"])
    through = PaidThrough.model_json_schema()["properties"]["kind"]["enum"]
    text = openai_provider.INSTRUCTIONS
    assert sorted(v for v in [*values, *through] if v not in text) == []
    units = PaidPeriod.model_json_schema()["$defs"]["CoverageLength"]["properties"]["unit"]["enum"]
    assert sorted(u for u in units if u not in text) == []
    for name in (
        "paid_through",
        "paid_period",
        "first_payment_on",
        "price_period",
        "allow_duplicate",
        "amount",
    ):
        assert name in text


def test_earlier_prompt_rules_are_kept():
    text = prompt()
    assert "A clear command" in text  # v5
    assert "Style of everything you write to the owner" in text  # v6
    assert "ALWAYS all" in text  # v4


def test_the_envelope_accepts_the_new_action_and_the_schema_stays_strict():
    from xboct.llm.schema import check_strict_subset, strict_schema

    envelope = IntentEnvelope.model_validate(
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_obligation",
                    "title": "Demo",
                    "kind": "coverage",
                    "paid_through": {"kind": "last_paid_day", "day": "2026-11-10"},
                }
            ],
        }
    )
    assert envelope.actions[0].type == "create_obligation"
    assert check_strict_subset(strict_schema(IntentEnvelope)) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "last_paid_day"},  # no day
        {"kind": "unclear"},
        {"kind": "month_only"},  # no month
        {"kind": "inclusive", "day": "2026-11-10"},  # unknown kind
    ],
)
def test_a_malformed_paid_through_is_refused_by_the_contract(bad):
    with pytest.raises(ValidationError):
        PaidThrough.model_validate(bad)


def test_the_docs_describe_the_action():
    root = Path(openai_provider.__file__).resolve().parents[3] / "docs" / "LLM_CONTRACT.md"
    text = root.read_text()
    assert "create_obligation" in text
    assert "paid_through" in text
