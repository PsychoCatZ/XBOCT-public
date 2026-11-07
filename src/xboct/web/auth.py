"""Web login through a one-time code from the owner's private Telegram chat.

- code: 10 random characters (unambiguous alphabet, ~50 bits), shown as XXXXX-XXXXX;
  only its SHA-256 is stored; valid 5 minutes; single use; a new code invalidates
  older unused ones;
- a wrong submission counts against every live code; after 5 failures they are dead;
- throttling: at most 10 failures per IP (hashed) and 30 overall per 15 minutes;
- session: 32 random bytes in an HttpOnly cookie, only the hash is stored; absolute
  expiry (AUTH_SESSION_TTL), revocable one by one or all at once;
- CSRF: HMAC of the session token, checked on every state-changing request together
  with the request origin; the login form uses a double-submit cookie.
Codes, tokens and IPs never go to logs.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xboct.application.service import Application
from xboct.storage.models import AuthCode, LoginAttempt, WebSession

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
CODE_TTL = timedelta(minutes=5)
MAX_FAILURES_PER_CODE = 5
THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_IP = 10
MAX_FAILURES_GLOBAL = 30
DEFAULT_SESSION_TTL = timedelta(days=7)
TMA_SESSION_TTL = timedelta(hours=12)
MAX_SESSIONS_PER_WINDOW = 20  # successful sign-ins (any way) per throttle window


class LoginThrottled(Exception):
    """Too many failed attempts: wait before trying again."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:5]}-{code[5:]}"


@dataclass(frozen=True)
class SessionInfo:
    id: int
    expires_at: datetime
    csrf: str


class AuthService:
    def __init__(self, app: Application, *, session_ttl: timedelta = DEFAULT_SESSION_TTL):
        self.app = app
        self.session_ttl = session_ttl

    def _now(self) -> datetime:
        return self.app.clock.now()

    @property
    def tma_ttl_seconds(self) -> int:
        return int(TMA_SESSION_TTL.total_seconds())

    # --- codes -----------------------------------------------------------------------

    async def issue_code(self) -> str:
        """New code for the owner (called only from the owner's private chat)."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        now = self._now()
        async with self.app.sessions() as session, session.begin():
            await session.execute(
                update(AuthCode)
                .where(AuthCode.used_at.is_(None), AuthCode.invalidated_at.is_(None))
                .values(invalidated_at=now)
            )
            session.add(AuthCode(code_hash=_sha(code), created_at=now, expires_at=now + CODE_TTL))
        return format_code(code)

    async def login(self, code: str, ip: str) -> str:
        """Returns a new session token. Raises LoginThrottled; ValueError on a bad code."""
        now = self._now()
        ip_hash = _sha(f"ip:{ip}")
        async with self.app.sessions() as session, session.begin():
            since = now - THROTTLE_WINDOW
            failures_ip = await session.scalar(
                select(func.count(LoginAttempt.id)).where(
                    LoginAttempt.at >= since,
                    LoginAttempt.ip_hash == ip_hash,
                    LoginAttempt.success.is_(False),
                )
            )
            failures_all = await session.scalar(
                select(func.count(LoginAttempt.id)).where(
                    LoginAttempt.at >= since, LoginAttempt.success.is_(False)
                )
            )
            if (failures_ip or 0) >= MAX_FAILURES_PER_IP or (
                failures_all or 0
            ) >= MAX_FAILURES_GLOBAL:
                raise LoginThrottled()
            live = list(
                await session.scalars(
                    select(AuthCode).where(
                        AuthCode.used_at.is_(None),
                        AuthCode.invalidated_at.is_(None),
                        AuthCode.expires_at > now,
                    )
                )
            )
            digest = _sha(normalize_code(code))
            match = next((c for c in live if hmac.compare_digest(c.code_hash, digest)), None)
            session.add(LoginAttempt(at=now, ip_hash=ip_hash, success=match is not None))
            if match is None:
                for item in live:
                    item.failures += 1
                    if item.failures >= MAX_FAILURES_PER_CODE:
                        item.invalidated_at = now
                failed = True
            else:
                match.used_at = now
                token = secrets.token_urlsafe(32)
                session.add(
                    WebSession(
                        token_hash=_sha(token),
                        created_at=now,
                        expires_at=now + self.session_ttl,
                        last_seen_at=now,
                    )
                )
                failed = False
        if failed:
            raise ValueError("bad code")
        return token

    # --- Mini App sign-in (initData is validated by the caller) ------------------------

    async def _tma_blocked(self, session: AsyncSession, now: datetime, ip_hash: str) -> bool:
        """Failures (this IP / overall) or session creation over the limit in the window."""
        since = now - THROTTLE_WINDOW
        failures_ip = await session.scalar(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.at >= since,
                LoginAttempt.ip_hash == ip_hash,
                LoginAttempt.success.is_(False),
            )
        )
        failures_all = await session.scalar(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.at >= since, LoginAttempt.success.is_(False)
            )
        )
        opened = await session.scalar(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.at >= since, LoginAttempt.success.is_(True)
            )
        )
        return (
            (failures_ip or 0) >= MAX_FAILURES_PER_IP
            or (failures_all or 0) >= MAX_FAILURES_GLOBAL
            or (opened or 0) >= MAX_SESSIONS_PER_WINDOW
        )

    async def tma_throttled(self, ip: str) -> bool:
        """True when failures or session creation are over the limit. It only decides whether
        a new session may be issued; it never stops the identity check (a signed message of
        another account must end the old session even then). The check that counts is the one
        inside `open_tma_session`, atomic with the insert."""
        async with self.app.sessions() as session:
            return await self._tma_blocked(session, self._now(), _sha(f"ip:{ip}"))

    async def tma_failed(self, ip: str) -> None:
        async with self.app.sessions() as session, session.begin():
            session.add(LoginAttempt(at=self._now(), ip_hash=_sha(f"ip:{ip}"), success=False))

    async def open_tma_session(
        self, ip: str, ttl: timedelta = TMA_SESSION_TTL, replace: str | None = None
    ) -> str:
        """New Mini App session (absolute expiry stored and checked by the server).

        The limit is checked in the same write transaction that inserts the attempt and the
        session (writers are serialised), so parallel requests cannot both take the last
        slot. `replace` is the token of this context's previous session: it is revoked in
        that same transaction, so a reopened Mini App holds one session, not a growing pile.
        Raises LoginThrottled.
        """
        now = self._now()
        ip_hash = _sha(f"ip:{ip}")
        token = secrets.token_urlsafe(32)
        async with self.app.sessions() as session, session.begin():
            if await self._tma_blocked(session, now, ip_hash):
                raise LoginThrottled()
            if replace:
                await session.execute(
                    update(WebSession)
                    .where(WebSession.token_hash == _sha(replace), WebSession.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            session.add(LoginAttempt(at=now, ip_hash=ip_hash, success=True))
            session.add(
                WebSession(
                    token_hash=_sha(token),
                    created_at=now,
                    expires_at=now + ttl,
                    last_seen_at=now,
                )
            )
        return token

    # --- sessions --------------------------------------------------------------------

    async def session_for(self, token: str | None) -> SessionInfo | None:
        if not token:
            return None
        now = self._now()
        async with self.app.sessions() as session, session.begin():
            row = await session.scalar(
                select(WebSession).where(WebSession.token_hash == _sha(token))
            )
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                return None
            row.last_seen_at = now
            return SessionInfo(row.id, row.expires_at, csrf_token(token))

    async def logout(self, token: str) -> None:
        async with self.app.sessions() as session, session.begin():
            await session.execute(
                update(WebSession)
                .where(WebSession.token_hash == _sha(token), WebSession.revoked_at.is_(None))
                .values(revoked_at=self._now())
            )

    async def revoke_all(self) -> int:
        async with self.app.sessions() as session, session.begin():
            result = await session.execute(
                update(WebSession)
                .where(WebSession.revoked_at.is_(None))
                .values(revoked_at=self._now())
            )
        return result.rowcount or 0

    async def active_sessions(self) -> int:
        now = self._now()
        async with self.app.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(WebSession.id)).where(
                        WebSession.revoked_at.is_(None), WebSession.expires_at > now
                    )
                )
                or 0
            )


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"xboct-csrf", hashlib.sha256).hexdigest()[:40]


def same_token(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))
