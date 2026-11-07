"""Telegram transport boundary and an in-memory fake used by all automatic tests.

The application never talks to Telegram directly: it stores inbox rows and outbox
messages; a transport only moves them. A send whose outcome is not known (timeout or
network error after the request left) is reported as `unknown`, never as success.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ErrorKind = Literal["retry_after", "transient", "permanent", "unknown"]


@dataclass(frozen=True)
class Button:
    text: str
    data: str


class TransportError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "", retry_after: float = 0) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail[:200]
        self.retry_after = retry_after


class Transport(Protocol):
    async def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict[str, Any]]: ...

    async def send_message(self, chat_id: int, text: str, buttons: list[list[Button]]) -> int:
        """Returns Telegram message_id. Raises TransportError."""
        ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...

    async def close(self) -> None: ...


@dataclass
class SentMessage:
    chat_id: int
    text: str
    buttons: list[list[Button]]
    message_id: int


@dataclass
class FakeTransport:
    """Deterministic stand-in for Telegram.

    - `queue_update(...)` makes updates available to get_updates; like Telegram, an
      update is returned again until a later call passes offset > its update_id.
    - `fail_next(kind)` makes the next send raise; `unknown_but_delivered=True` records
      the message as delivered and still raises `unknown` (lost response).
    """

    updates: list[dict[str, Any]] = field(default_factory=list)
    sent: list[SentMessage] = field(default_factory=list)
    callbacks_answered: list[tuple[str, str]] = field(default_factory=list)
    offsets_seen: list[int | None] = field(default_factory=list)
    _failures: list[tuple[ErrorKind, float, bool]] = field(default_factory=list)
    _next_message_id: int = 1000
    on_send: Callable[[SentMessage], None] | None = None

    def queue_update(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    def fail_next(
        self, kind: ErrorKind, *, retry_after: float = 0, unknown_but_delivered: bool = False
    ) -> None:
        self._failures.append((kind, retry_after, unknown_but_delivered))

    async def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict[str, Any]]:
        self.offsets_seen.append(offset)
        if offset is not None:
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        return list(self.updates)

    async def send_message(self, chat_id: int, text: str, buttons: list[list[Button]]) -> int:
        if self._failures:
            kind, retry_after, delivered = self._failures.pop(0)
            if delivered:
                self._record(chat_id, text, buttons)
            raise TransportError(kind, "fake failure", retry_after=retry_after)
        return self._record(chat_id, text, buttons).message_id

    def _record(self, chat_id: int, text: str, buttons: list[list[Button]]) -> SentMessage:
        self._next_message_id += 1
        message = SentMessage(chat_id, text, buttons, self._next_message_id)
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send(message)
        return message

    async def answer_callback(self, callback_id: str, text: str) -> None:
        self.callbacks_answered.append((callback_id, text))

    async def close(self) -> None:
        return None

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]
