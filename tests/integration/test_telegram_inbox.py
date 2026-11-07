"""Inbox: store before ack, owner checks, repeated updates, crash recovery (A16, A17, A26)."""

from datetime import date

import pytest

from tests.integration.rt import OWNER, STRANGER
from xboct.application import commands as c
from xboct.scheduling.state import TELEGRAM_OFFSET, get_state
from xboct.storage.models import CommandReceipt, Task, TelegramInbox
from xboct.telegram import bot as bot_module


def yulia(bot) -> None:
    bot.provider.add(
        "ответить Юле в течение недели",
        {
            "kind": "actions",
            "actions": [
                {
                    "type": "create_task",
                    "title": "Ответить Юле",
                    "due": {"date": {"kind": "within_week"}},
                }
            ],
        },
    )


async def test_message_is_stored_processed_and_answered(bot):
    yulia(bot)
    update_id = bot.say("ответить Юле в течение недели")
    report = await bot.tick()
    assert report.polled == 1
    assert report.processed == 1
    assert bot.sent[-1].startswith("Записал дело «Ответить Юле»: срок 15 октября")
    assert bot.transport.sent[-1].chat_id == OWNER
    row = (await bot.rows(TelegramInbox))[0]
    assert row.status == "done"
    assert row.payload["message"]["text"] == "ответить Юле в течение недели"
    async with bot.app.sessions() as session:
        assert await get_state(session, TELEGRAM_OFFSET) == {"offset": update_id + 1}
    # The next poll acknowledges the update by passing the larger offset.
    await bot.tick()
    assert bot.transport.offsets_seen[-1] == update_id + 1


async def test_redelivered_update_applies_and_answers_once(bot):
    # A16: Telegram re-sends an update whose offset was never acknowledged.
    yulia(bot)
    update_id = bot.say("ответить Юле в течение недели")
    await bot.tick()
    bot.say("ответить Юле в течение недели", update_id=update_id)  # same update again
    await bot.tick()
    assert len(await bot.rows(TelegramInbox)) == 1
    assert len(await bot.rows(Task)) == 1
    assert len([t for t in bot.sent if t.startswith("Записал дело")]) == 1


async def test_crash_after_commit_before_inbox_done(bot, monkeypatch):
    # A17: the command committed, then the process died before the update was marked
    # done and before the reply was queued. After restart: no second task, one reply.
    yulia(bot)
    original = bot_module.BotHandler._reply

    async def crash(self, *args, **kwargs):
        raise RuntimeError("process died")

    monkeypatch.setattr(bot_module.BotHandler, "_reply", crash)
    update_id = bot.say("ответить Юле в течение недели")
    await bot.tick()
    assert len(await bot.rows(Task)) == 1
    assert (await bot.rows(TelegramInbox))[0].status == "processing"
    assert bot.sent == []

    monkeypatch.setattr(bot_module.BotHandler, "_reply", original)
    await bot.restart()
    await bot.tick()
    assert len(await bot.rows(Task)) == 1
    receipt = (
        await bot.rows(CommandReceipt, CommandReceipt.command_id == f"tg-update:{update_id}")
    )[0]
    assert receipt.status == "applied"
    assert len([t for t in bot.sent if t.startswith("Записал дело")]) == 1
    assert (await bot.rows(TelegramInbox))[0].status == "done"


async def test_stored_but_unprocessed_updates_survive_restart(bot, monkeypatch):
    yulia(bot)

    async def no_processing(*args, **kwargs):
        return 0

    monkeypatch.setattr("xboct.scheduling.runtime.process_pending", no_processing)
    bot.say("ответить Юле в течение недели")
    await bot.tick()
    assert (await bot.rows(TelegramInbox))[0].status == "received"
    monkeypatch.undo()
    await bot.restart()
    await bot.tick()
    assert len(await bot.rows(Task)) == 1


async def test_stranger_is_ignored_without_storing_content(bot):
    # A26: read/write denied; nothing applied, nothing answered, content not kept.
    yulia(bot)
    bot.say("ответить Юле в течение недели", sender=STRANGER)
    bot.say("/today", sender=STRANGER)
    await bot.tick()
    assert await bot.rows(Task) == []
    assert bot.sent == []
    rows = await bot.rows(TelegramInbox)
    assert {r.status for r in rows} == {"rejected"}
    assert all(r.payload == {"redacted": True} for r in rows)


async def test_owner_in_group_chat_is_not_trusted(bot):
    yulia(bot)
    bot.say("ответить Юле в течение недели", chat_type="group")
    await bot.tick()
    assert await bot.rows(Task) == []
    assert bot.sent == []


async def test_forged_callbacks_are_refused(bot):
    result = await bot.ok(c.CreateTask(title="Ответить", due_date=date(2026, 10, 15)))
    task_id = result.data["task_id"]
    bot.press(f"dt:{task_id}:1", sender=STRANGER)  # someone else
    bot.press(f"dt:{task_id}:7")  # owner, but a version that never existed
    bot.press("dt:../../etc:1")  # garbage
    await bot.tick()
    assert (await bot.x.get(Task, task_id)).status == "open"
    assert ("cbq103", "Кнопка устарела") in bot.transport.callbacks_answered
    assert any("изменилась" in t for t in bot.sent)


async def test_commands_without_language_model(bot):
    await bot.ok(c.CreateTask(title="Ответить Юле", due_date=date(2026, 10, 9)))
    bot.say("/today")
    bot.say("/payments")
    bot.say("/help")
    await bot.tick()
    assert "Ответить Юле" in bot.sent[0]
    assert bot.sent[1] == "Платежей пока нет."
    assert "/undo" in bot.sent[2]


async def test_model_failure_answers_honestly(bot):
    bot.say("непонятное")  # no fixture -> provider error
    await bot.tick()
    assert bot.sent == ["Не разобрал сообщение; данные не менял. Можно сделать через форму."]
    assert await bot.rows(Task) == []


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text(bot, text):
    bot.say(text)
    await bot.tick()
    assert bot.sent == ["Пока понимаю только текст."]
