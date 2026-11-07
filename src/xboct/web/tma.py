"""Telegram Mini App sign-in: validation of `initData` (HMAC, bot-owner algorithm).

Algorithm (Telegram «Validating data received via the Mini App»): the received query
string is split into fields; the data-check-string is every field except `hash`, sorted by
key, as `key=value` lines joined with `\\n` (values exactly as decoded, `user` is NOT
re-serialised); secret = HMAC_SHA256(key="WebAppData", msg=bot_token); the signature is
HMAC_SHA256(key=secret, msg=data_check_string) in lowercase hex. The separate Ed25519
`signature` algorithm for third parties is not used; the field stays in the string as
received.

On top of the signature the server requires: a `user` object whose integer `id` is exactly the
owner, a fresh `auth_date` (at most 15 minutes old, at most 60 seconds ahead), no
duplicate keys, and a body that fits the size limit. `initDataUnsafe`, the presence of
`Telegram.WebApp` or any "mode" parameter grant nothing. Nothing from the input is ever
put into an error message or a log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

MAX_BODY_BYTES = 16 * 1024
MAX_AGE = timedelta(minutes=15)
MAX_FUTURE = timedelta(seconds=60)
MAX_FIELDS = 32
_HASH = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")


@dataclass(frozen=True)
class TmaConfig:
    """What the server needs to accept Mini App sign-ins (the token is never printed)."""

    bot_token: str = field(repr=False)
    owner_id: int


class InitDataError(Exception):
    """`code` is a fixed word (malformed, signature, stale, future, forbidden)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TmaUser:
    id: int
    auth_date: datetime


def secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")


def sign(fields: dict[str, str], bot_token: str) -> str:
    """The hash Telegram would send for `fields` (used by tests and the browser check)."""
    digest = hmac.new(
        secret_key(bot_token), data_check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    return digest


def parse_fields(raw: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=MAX_FIELDS,
        )
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:  # empty or duplicate key: the signed text would be ambiguous
            raise InitDataError("malformed")
        fields[key] = value
    return fields


def validate_init_data(raw: str, bot_token: str, owner_id: int, now: datetime) -> TmaUser:
    """Returns the owner or raises InitDataError. Order: structure, signature, time, user."""
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_BODY_BYTES:
        raise InitDataError("malformed")
    fields = parse_fields(raw)
    received = fields.get("hash", "")
    if not _HASH.match(received) or "auth_date" not in fields or "user" not in fields:
        raise InitDataError("malformed")
    expected = sign(fields, bot_token)
    if not hmac.compare_digest(received, expected):
        raise InitDataError("signature")
    # Signed by Telegram for this bot from here on. Whose it is is decided before how old it
    # is: a signed message of another account is "forbidden" whatever its age.
    try:
        user = json.loads(fields["user"])
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise InitDataError("malformed")
    if user_id != owner_id:
        raise InitDataError("forbidden")
    stamp = fields["auth_date"]
    if not _DIGITS.match(stamp):
        raise InitDataError("malformed")
    auth_date = datetime.fromtimestamp(int(stamp), UTC)
    if now - auth_date > MAX_AGE:
        raise InitDataError("stale")
    if auth_date - now > MAX_FUTURE:
        raise InitDataError("future")
    return TmaUser(user_id, auth_date)
