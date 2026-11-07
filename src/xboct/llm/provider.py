"""The narrow provider interface. No DB, SQL, shell, HTTP or Telegram access here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from xboct.llm.contract import IntentEnvelope, IntentRequest

FailureKind = Literal[
    "unavailable", "timeout", "refusal", "incomplete", "invalid", "no_fixture", "daily_limit"
]


@dataclass(frozen=True)
class ProviderMeta:
    """Bookkeeping for one proposal: what ran and what it cost. Never the prompt text."""

    provider: str
    model: str | None = None
    prompt_version: str | None = None
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None


class IntentProviderError(Exception):
    """Controlled provider failure: nothing is mutated, forms and reminders keep working."""

    def __init__(
        self,
        kind: FailureKind,
        detail: str = "",
        meta: ProviderMeta | None = None,
        diagnostic: dict[str, Any] | None = None,
    ):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.meta = meta
        # Structure of what was wrong (validation errors, the proposal's outline), for the
        # synthetic eval report only. Never persisted by the application or logged.
        self.diagnostic = diagnostic


class IntentProvider(Protocol):
    name: str

    async def propose(self, request: IntentRequest) -> IntentEnvelope: ...

    async def propose_with_meta(
        self, request: IntentRequest
    ) -> tuple[IntentEnvelope, ProviderMeta]: ...
