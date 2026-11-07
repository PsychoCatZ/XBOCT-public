"""Real aiogram updates must retain Telegram wire keys at the inbox boundary."""

from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update

from xboct.telegram.aiogram_transport import AiogramTransport
from xboct.telegram.inbox import describe, is_owner


@pytest.mark.parametrize("kind", ["message", "callback"])
async def test_real_updates_keep_sender_alias_and_owner_identity(kind):
    owner = 123456789
    sender = {"id": owner, "is_bot": False, "first_name": "Owner"}
    message = {
        "message_id": 1,
        "date": 1791464400,
        "from": sender,
        "chat": {"id": owner, "type": "private"},
        "text": "/login",
    }
    raw = {"update_id": 42}
    if kind == "message":
        raw["message"] = message
    else:
        raw["callback_query"] = {
            "id": "callback-1",
            "from": sender,
            "chat_instance": "instance-1",
            "message": message,
            "data": "example",
        }
    update = Update.model_validate(raw)
    transport = AiogramTransport("123456789:" + "A" * 35)
    transport._bot.get_updates = AsyncMock(return_value=[update])
    try:
        [wire] = await transport.get_updates(None, 0)
    finally:
        await transport.close()

    assert describe(wire) == (kind, owner)
    assert is_owner(wire, owner)
    assert not is_owner(wire, owner + 1)
    entity = wire["message"] if kind == "message" else wire["callback_query"]
    assert "from" in entity
    assert "from_user" not in entity
