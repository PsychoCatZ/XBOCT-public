"""Domain errors. Each carries a stable code and a human message (ru)."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """A command cannot be applied; nothing is changed."""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class ValidationFailed(DomainError):
    code = "validation_error"


class NotFound(DomainError):
    code = "not_found"


class InvalidState(DomainError):
    code = "invalid_state"


class VersionConflict(DomainError):
    code = "version_conflict"


class UndoUnavailable(DomainError):
    code = "undo_unavailable"


class IdempotencyConflict(DomainError):
    """The same command_id was reused for a different payload."""

    code = "idempotency_conflict"


class NeedsClarification(DomainError):
    """The request is not specific enough to apply safely."""

    code = "needs_clarification"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        options: list[dict[str, Any]] | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, code=code, **details)
        self.options = options or []
