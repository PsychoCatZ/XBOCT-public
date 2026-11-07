"""Real Telegram transport over aiogram 3 (Bot API long polling).

Not exercised against the network by automatic tests; error classification is unit
tested. The token is never logged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from xboct.telegram.transport import Button, TransportError

ALLOWED_UPDATES = ["message", "callback_query"]
DEFAULT_REQUEST_TIMEOUT = 20  # seconds for one Bot API call (a send is one request)


def classify_send_error(exc: BaseException) -> TransportError:
    """Map aiogram/network errors of sendMessage to our retry classes.

    A timeout or network error may happen after Telegram accepted the message, so it is
    `unknown`, not transient: retrying immediately could duplicate the message.
    """
    if isinstance(exc, TelegramRetryAfter):
        return TransportError("retry_after", "flood control", retry_after=exc.retry_after)
    if isinstance(exc, TelegramServerError):
        return TransportError("transient", type(exc).__name__)
    if isinstance(
        exc,
        TelegramForbiddenError | TelegramUnauthorizedError | TelegramBadRequest | TelegramNotFound,
    ):
        return TransportError("permanent", type(exc).__name__)
    if isinstance(exc, TelegramNetworkError | TimeoutError | asyncio.TimeoutError):
        return TransportError("unknown", type(exc).__name__)
    return TransportError("unknown", type(exc).__name__)


def keyboard(buttons: list[list[Button]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
            for row in buttons
        ]
    )


class AiogramTransport:
    def __init__(self, token: str, *, request_timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self._bot = Bot(token=token)
        self._timeout = request_timeout

    async def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict[str, Any]]:
        try:
            updates = await self._bot.get_updates(
                offset=offset,
                timeout=poll_seconds,
                allowed_updates=ALLOWED_UPDATES,
                request_timeout=poll_seconds + 10,
            )
        except TelegramRetryAfter as exc:
            raise TransportError("retry_after", "getUpdates", retry_after=exc.retry_after) from exc
        except Exception as exc:
            raise TransportError("transient", f"getUpdates {type(exc).__name__}") from exc
        return [u.model_dump(mode="json", by_alias=True, exclude_none=True) for u in updates]

    async def send_message(self, chat_id: int, text: str, buttons: list[list[Button]]) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard(buttons),
                request_timeout=int(self._timeout),
            )
        except Exception as exc:
            raise classify_send_error(exc) from exc
        return message.message_id

    async def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self._bot.answer_callback_query(callback_id, text=text[:200])
        except Exception:  # best effort: the result also goes out as a normal reply
            return None

    async def close(self) -> None:
        await self._bot.session.close()
