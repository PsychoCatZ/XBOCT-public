from datetime import date

import pytest
from pydantic import ValidationError

from xboct.application import commands as c
from xboct.llm.contract import IntentEnvelope
from xboct.storage.models import Obligation, Payment


async def seed_status(x):
    for title, until in (
        ("ChatGPT", date(2026, 11, 9)),
        ("Claude", date(2026, 10, 16)),
        ("Gemini", date(2026, 10, 29)),
    ):
        await x.ok(c.CreateObligation(title=title, kind="coverage", coverage_until=until))
    archived = await x.ok(
        c.CreateObligation(title="Claude", kind="coverage", coverage_until=date(2027, 1, 1))
    )
    ob = await x.get(Obligation, archived.data["obligation_id"])
    await x.ok(c.ArchiveObligation(obligation_id=ob.id, expected_version=ob.version))


@pytest.mark.parametrize(
    "phrase", ["А оплачено что?", "Что оплачено?", "Ты мне скажи, что оплачено!"]
)
async def test_current_coverage_is_not_a_transaction_journal(x, phrase):
    await seed_status(x)
    before = [(o.id, o.coverage_until, o.active, o.version) for o in await x.all(Obligation)]
    x.provider.add(
        phrase,
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "payment_status": "covered",
            },
        },
    )
    result = await x.say(phrase)
    assert result.status == "answered"
    assert "Сейчас оплачено" in result.message
    assert result.message.count("Claude") == 1
    for title, day in (("ChatGPT", "8 ноября"), ("Claude", "15 октября"), ("Gemini", "28 октября")):
        assert title in result.message
        assert day in result.message
    assert "История" not in result.message
    assert "продлить" not in result.message
    assert before == [
        (o.id, o.coverage_until, o.active, o.version) for o in await x.all(Obligation)
    ]
    assert await x.all(Payment) == []


async def test_expired_service_is_not_currently_paid(x):
    await x.ok(
        c.CreateObligation(title="Истёкший", kind="coverage", coverage_until=date(2026, 10, 8))
    )
    x.provider.add(
        "сейчас",
        {
            "kind": "query",
            "query": {
                "kind": "payments",
                "payment_status": "covered",
            },
        },
    )
    result = await x.say("сейчас")
    assert "Истёкший" not in result.message


async def test_social_frustration_does_not_dump_payments_or_change_state(x):
    await seed_status(x)
    x.provider.add(
        "Какой же ты тяжёлый", {"kind": "conversation", "conversation_kind": "frustration"}
    )
    result = await x.say("Какой же ты тяжёлый")
    assert result.status == "answered"
    assert "механически" in result.message
    assert "ChatGPT" not in result.message
    assert "Claude" not in result.message
    assert "Gemini" not in result.message
    assert len(await x.all(Obligation)) == 4
    assert await x.all(Payment) == []


def test_conversation_cannot_smuggle_a_query_or_mutation():
    with pytest.raises(ValidationError):
        IntentEnvelope.model_validate(
            {
                "kind": "conversation",
                "conversation_kind": "thanks",
                "actions": [{"type": "create_task", "title": "Не создавать"}],
            }
        )
    with pytest.raises(ValidationError):
        IntentEnvelope.model_validate(
            {"kind": "conversation", "conversation_kind": "thanks", "query": {"kind": "payments"}}
        )
