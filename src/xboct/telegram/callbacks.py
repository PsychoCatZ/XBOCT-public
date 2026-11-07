"""Semantic callback tokens (Telegram callback_data, max 64 bytes).

A token names the entity, its version and the action, so a repeated click maps to the
same command_id ("cb:<token>") and cannot apply twice. Tokens are untrusted input:
the server checks the owner, the entity and its version before doing anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Action = Literal["done_task", "done_occ", "paid", "snooze", "confirm", "cancel"]

_ID = r"[0-9a-f]{32}"
_PATTERNS: dict[Action, re.Pattern[str]] = {
    "done_task": re.compile(rf"^dt:({_ID}):(\d{{1,9}})$"),
    "done_occ": re.compile(rf"^do:({_ID}):(\d{{1,9}})$"),
    "paid": re.compile(rf"^pd:({_ID}):(\d{{1,9}})$"),
    "snooze": re.compile(rf"^sz:({_ID})$"),
    "confirm": re.compile(rf"^pc:({_ID})$"),
    "cancel": re.compile(rf"^px:({_ID})$"),
}
_PREFIX: dict[Action, str] = {
    "done_task": "dt",
    "done_occ": "do",
    "paid": "pd",
    "snooze": "sz",
    "confirm": "pc",
    "cancel": "px",
}


@dataclass(frozen=True)
class Token:
    action: Action
    entity_id: str
    version: int | None = None

    def encode(self) -> str:
        text = f"{_PREFIX[self.action]}:{self.entity_id}"
        if self.version is not None:
            text += f":{self.version}"
        if len(text.encode()) > 64:
            raise ValueError("callback token too long")
        return text


def parse(data: str) -> Token | None:
    for action, pattern in _PATTERNS.items():
        match = pattern.match(data)
        if match:
            version = int(match.group(2)) if match.lastindex and match.lastindex > 1 else None
            return Token(action, match.group(1), version)
    return None
