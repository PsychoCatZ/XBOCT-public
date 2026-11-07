"""Routing of stored owner updates to application commands; replies go to the outbox.

Buttons work without the language model. Every update maps to a stable command_id:
messages -> "tg-update:<update_id>", buttons -> "cb:<semantic token>".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult
from xboct.application.intents import IntentService
from xboct.application.queries import agenda, agenda_text, payments, payments_text
from xboct.application.service import Application
from xboct.application.uow import Uow
from xboct.llm.contract import MAX_CONTEXT_MESSAGES, ContextMessage
from xboct.scheduling.outbox import enqueue
from xboct.storage.models import NotificationJob, Occurrence, OutboxMessage, Task, TelegramInbox
from xboct.telegram.callbacks import Token, parse
from xboct.telegram.inbox import message_time
from xboct.telegram.transport import Button, Transport
from xboct.web.auth import AuthService

HELP = (
    "Я XBOCT. Пиши дела и платежи обычным текстом.\n"
    "/today — что сегодня, на неделе и просрочено\n"
    "/payments — платежи, оплаченные периоды и запас средств\n"
    "/undo — отменить последнее действие\n"
    "/login — одноразовый код для входа в веб"
)


class BotHandler:
    def __init__(
        self,
        app: Application,
        intents: IntentService,
        transport: Transport,
        owner_id: int,
        auth: AuthService | None = None,
    ) -> None:
        self.app = app
        self.intents = intents
        self.transport = transport
        self.owner_id = owner_id
        self.auth = auth

    async def __call__(self, row: TelegramInbox) -> dict[str, Any]:
        if row.kind == "message":
            return await self._message(row)
        return await self._callback(row)

    # --- messages --------------------------------------------------------------------

    async def _message(self, row: TelegramInbox) -> dict[str, Any]:
        message = row.payload["message"]
        text = (message.get("text") or "").strip()
        received_at = message_time(row.payload, row.received_at)
        ctx = CommandContext(
            command_id=f"tg-update:{row.update_id}",
            source="telegram_update",
            received_at=received_at,
            source_key=str(message.get("message_id")),
        )
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        if not text:
            return await self._reply(row, "Пока понимаю только текст.")
        if command in ("/start", "/help"):
            return await self._reply(row, HELP)
        if command == "/today":
            return await self._reply(row, await self.app.read(self._agenda))
        if command == "/payments":
            return await self._reply(row, await self.app.read(self._payments))
        if command == "/login":
            if self.auth is None:
                return await self._reply(row, "Веб-вход не настроен.")
            code = await self.auth.issue_code()
            return await self._reply(
                row,
                f"Код для входа в веб: {code}\nДействует 5 минут, одноразовый. "
                "Если вход запрашивал не ты — просто ничего не делай.",
                sensitive=True,
            )
        if command == "/undo":
            result = await self.app.execute(c.UndoCommand(), ctx)
            return await self._reply(row, result.message, result)
        history = await self.app.read(lambda uow: recent_dialog(uow, row.update_id))
        result = await self.intents.handle_message(text, ctx, history)
        buttons: list[list[Button]] = []
        if result.status == "preview" and "pending_id" in result.data:
            pending_id = result.data["pending_id"]
            buttons = [
                [
                    Button("Подтвердить", Token("confirm", pending_id).encode()),
                    Button("Отмена", Token("cancel", pending_id).encode()),
                ]
            ]
        return await self._reply(row, result.message, result, buttons)

    @staticmethod
    async def _agenda(uow: Uow) -> str:
        return agenda_text(uow, await agenda(uow))

    @staticmethod
    async def _payments(uow: Uow) -> str:
        return payments_text(uow, await payments(uow))

    # --- buttons ---------------------------------------------------------------------

    async def _callback(self, row: TelegramInbox) -> dict[str, Any]:
        query = row.payload["callback_query"]
        data = query.get("data") or ""
        token = parse(data)
        if token is None:
            await self.transport.answer_callback(query["id"], "Кнопка устарела")
            return {"status": "bad_token"}
        command = await self.app.read(lambda uow: self._command_for(uow, token))
        if command is None:
            await self.transport.answer_callback(query["id"], "Запись не найдена")
            return await self._reply(row, "Эта кнопка больше не актуальна: запись не найдена.")
        ctx = CommandContext(
            command_id=f"cb:{data}",
            source="telegram_callback",
            received_at=row.received_at,
            source_key=str(row.update_id),
        )
        result = await self.app.execute(command, ctx)
        await self.transport.answer_callback(
            query["id"], "Готово" if result.status == "applied" else "Не применил"
        )
        return await self._reply(row, result.message, result)

    async def _command_for(self, uow: Uow, token: Token) -> c.Command | None:
        session = uow.session
        if token.action == "done_task":
            return c.CompleteTask(task_id=token.entity_id, expected_version=token.version or 1)
        if token.action == "done_occ":
            occ = await session.get(Occurrence, token.entity_id)
            if occ is None or occ.owner_type != "task":
                return None
            task = await session.get(Task, occ.owner_id)
            if task is None:
                return None
            return c.CompleteTask(
                task_id=task.id,
                expected_version=task.version,
                occurrence_id=occ.id,
                occurrence_version=token.version,
            )
        if token.action == "paid":
            occ = await session.get(Occurrence, token.entity_id)
            if occ is None or occ.owner_type != "obligation":
                return None
            return c.MarkPayment(
                obligation_id=occ.owner_id,
                occurrences=[c.OccurrenceRef(id=occ.id, expected_version=token.version or 1)],
            )
        if token.action == "snooze":
            job = await session.get(NotificationJob, token.entity_id)
            if job is None:
                return None
            return c.SnoozeNotification(
                subject_type=job.subject_type,  # type: ignore[arg-type]
                subject_id=job.subject_id,
                occurrence_id=job.occurrence_id,
                until=uow.now + timedelta(minutes=uow.settings.later_minutes),
                explicit=False,
            )
        if token.action == "confirm":
            return c.ConfirmPendingIntent(pending_id=token.entity_id)
        return c.CancelPendingIntent(pending_id=token.entity_id)

    # --- replies ---------------------------------------------------------------------

    async def _reply(
        self,
        row: TelegramInbox,
        text: str,
        result: CommandResult | None = None,
        buttons: list[list[Button]] | None = None,
        *,
        sensitive: bool = False,
    ) -> dict[str, Any]:
        async with self.app.sessions() as session, session.begin():
            settings = await self.app.settings_in(session)
            now = self.app.clock.now()
            ctx = CommandContext(command_id="reply", source="system", received_at=now)
            uow = Uow(session, ctx, now, settings)
            message = await enqueue(
                session,
                kind="reply",
                chat_id=self.owner_id,
                text=text,
                buttons=buttons,
                dedup_key=f"reply:{row.update_id}",
                now=now,
                local_day=uow.today_now,
                sensitive=sensitive,
            )
        return {
            "reply_outbox_id": message.id,
            "command_status": result.status if result else None,
            "code": result.code if result else None,
        }


async def recent_dialog(uow: Uow, before_update_id: int) -> list[ContextMessage]:
    """Recent owner messages and our replies, oldest first (bounded again by the pipeline).

    Only text the owner wrote in this chat and what we answered; notes and other stored
    data are never added here.
    """
    limit = MAX_CONTEXT_MESSAGES
    rows = await uow.session.scalars(
        select(TelegramInbox)
        .where(
            TelegramInbox.kind == "message",
            TelegramInbox.status == "done",
            TelegramInbox.update_id < before_update_id,
        )
        .order_by(TelegramInbox.update_id.desc())
        .limit(limit)
    )
    items: list[ContextMessage] = []
    for inbox in rows:
        text = ((inbox.payload or {}).get("message") or {}).get("text")
        if text:
            items.append(ContextMessage(role="user", text=text, at=inbox.received_at))
    replies = await uow.session.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.kind == "reply",
            OutboxMessage.status != "cancelled",
            OutboxMessage.sensitive.is_(False),  # never show a login code to the model
        )
        .order_by(OutboxMessage.created_at.desc())
        .limit(limit)
    )
    for reply in replies:
        if reply.dedup_key != f"reply:{before_update_id}":
            items.append(ContextMessage(role="assistant", text=reply.text, at=reply.created_at))
    items.sort(key=lambda m: m.at)
    return items[-limit:]
