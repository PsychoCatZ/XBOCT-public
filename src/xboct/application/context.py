"""Command context and the result returned to every caller (Telegram, web, CLI, tests)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from xboct.domain.timeutil import require_aware

Source = Literal["telegram_update", "telegram_callback", "web", "system", "cli", "test"]
ResultStatus = Literal["applied", "answered", "preview", "clarification", "rejected"]


@dataclass(frozen=True)
class CommandContext:
    """Who asked and when. received_at anchors all relative dates of the request.

    command_id is the idempotency key: Telegram update_id, a semantic callback token
    (entity + version + action) or a web idempotency key. Repeats return the stored result.
    """

    command_id: str
    source: Source
    received_at: datetime
    source_key: str | None = None

    def __post_init__(self) -> None:
        if not self.command_id or len(self.command_id) > 128:
            raise ValueError("command_id must be 1..128 chars")
        object.__setattr__(self, "received_at", require_aware(self.received_at))

    @classmethod
    def new(
        cls,
        received_at: datetime,
        *,
        source: Source = "test",
        command_id: str | None = None,
        source_key: str | None = None,
    ) -> CommandContext:
        return cls(
            command_id=command_id or f"{source}:{uuid.uuid4().hex}",
            source=source,
            received_at=received_at,
            source_key=source_key,
        )


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    command_type: str
    status: ResultStatus
    message: str
    code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, Any]] = Field(default_factory=list)
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("applied", "answered", "preview")


@dataclass
class Outcome:
    """What a handler returns; turned into a CommandResult after flush."""

    message: str
    data: dict[str, Any] = field(default_factory=dict)
    status: ResultStatus = "applied"
    undoable: bool = True
