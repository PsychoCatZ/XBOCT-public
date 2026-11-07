"""Stage 2 harness: the real Runtime on a disk database, FakeTransport, fixed clock."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from tests.conftest import Harness
from xboct.application import commands as c
from xboct.application.context import CommandResult
from xboct.application.service import Application
from xboct.domain.clock import FixedClock
from xboct.llm.fixture import FixtureIntentProvider
from xboct.scheduling.runtime import Runtime, TickReport
from xboct.storage.models import Base, OutboxMessage
from xboct.telegram.transport import FakeTransport
from xboct.web.auth import AuthService

OWNER = 4242
STRANGER = 777


class Bot:
    def __init__(
        self, db_url: str, lock: Path, clock: FixedClock, provider: FixtureIntentProvider
    ) -> None:
        self.db_url = db_url
        self.lock = lock
        self.clock = clock
        self.provider = provider
        self.transport = FakeTransport()
        self.app: Application
        self.runtime: Runtime
        self.x: Harness
        self._update_id = 100

    async def start(self) -> None:
        self.app = await Application.open(self.db_url, self.clock)
        self.auth = AuthService(self.app)
        self.runtime = Runtime(
            self.app,
            self.transport,
            self.provider,
            OWNER,
            lock_path=self.lock,
            poll_timeout=0,
            auth=self.auth,
        )
        await self.runtime.start()
        self.x = Harness(self.app, self.clock, self.provider)

    async def stop(self) -> None:
        await self.runtime.stop()
        await self.app.close()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def tick(self) -> TickReport:
        return await self.runtime.tick()

    async def ok(self, command: c.Command, command_id: str | None = None) -> CommandResult:
        return await self.x.ok(command, command_id)

    # --- Bot API shaped updates ------------------------------------------------------

    def next_id(self) -> int:
        self._update_id += 1
        return self._update_id

    def say(
        self,
        text: str,
        *,
        sender: int = OWNER,
        chat_type: str = "private",
        update_id: int | None = None,
    ) -> int:
        update_id = update_id or self.next_id()
        self.transport.queue_update(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id * 10,
                    "date": int(self.clock.now().timestamp()),
                    "chat": {"id": sender if chat_type == "private" else -100, "type": chat_type},
                    "from": {"id": sender, "is_bot": False, "first_name": "x"},
                    "text": text,
                },
            }
        )
        return update_id

    def press(self, data: str, *, sender: int = OWNER, update_id: int | None = None) -> int:
        update_id = update_id or self.next_id()
        self.transport.queue_update(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": f"cbq{update_id}",
                    "from": {"id": sender, "is_bot": False, "first_name": "x"},
                    "chat_instance": "ci",
                    "data": data,
                    "message": {
                        "message_id": 1,
                        "date": 0,
                        "chat": {"id": sender, "type": "private"},
                    },
                },
            }
        )
        return update_id

    # --- inspection ------------------------------------------------------------------

    @property
    def sent(self) -> list[str]:
        return self.transport.texts

    def buttons(self, index: int = -1) -> list[tuple[str, str]]:
        return [(b.text, b.data) for row in self.transport.sent[index].buttons for b in row]

    async def outbox(self, *where: Any) -> list[OutboxMessage]:
        async with self.app.sessions() as session:
            return list(
                await session.scalars(
                    select(OutboxMessage).where(*where).order_by(OutboxMessage.created_at)
                )
            )

    async def rows(self, model: type[Base], *where: Any) -> list[Any]:
        async with self.app.sessions() as session:
            return list(await session.scalars(select(model).where(*where)))


def local_now(bot: Bot, tz: ZoneInfo) -> datetime:
    return bot.clock.now().astimezone(tz)
