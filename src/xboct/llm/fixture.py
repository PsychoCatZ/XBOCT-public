"""Fixture IntentProvider: canned proposals for exact messages. Not NL understanding.

Tests register what a model would propose for a phrase; the application pipeline is
then exercised for real (validation, date resolution, versions, preview, apply).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from xboct.llm.contract import IntentEnvelope, IntentRequest
from xboct.llm.provider import IntentProviderError, ProviderMeta

FixtureResponse = (
    IntentEnvelope
    | dict[str, Any]
    | Callable[[IntentRequest], IntentEnvelope | dict[str, Any]]
    | IntentProviderError
)


def normalize(message: str) -> str:
    return " ".join(message.split()).casefold()


class FixtureIntentProvider:
    name = "fixture"

    def __init__(self, fixtures: dict[str, FixtureResponse] | None = None) -> None:
        self._fixtures: dict[str, FixtureResponse] = {}
        self.calls: list[IntentRequest] = []
        for message, response in (fixtures or {}).items():
            self.add(message, response)

    def add(self, message: str, response: FixtureResponse) -> None:
        self._fixtures[normalize(message)] = response

    async def propose_with_meta(
        self, request: IntentRequest
    ) -> tuple[IntentEnvelope, ProviderMeta]:
        return await self.propose(request), ProviderMeta(provider=self.name, model="fixture")

    async def propose(self, request: IntentRequest) -> IntentEnvelope:
        self.calls.append(request)
        response = self._fixtures.get(normalize(request.message))
        if response is None:
            raise IntentProviderError("no_fixture", "no canned proposal for this message")
        if isinstance(response, IntentProviderError):
            raise response
        if callable(response):
            response = response(request)
        raw = response.model_dump(mode="json") if isinstance(response, IntentEnvelope) else response
        try:
            # Round-trip through JSON like a real adapter would receive tool arguments.
            return IntentEnvelope.model_validate(raw)
        except ValidationError as exc:
            raise IntentProviderError("invalid", f"{exc.error_count()} schema errors") from exc
