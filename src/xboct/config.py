"""Environment configuration (docs/OPERATIONS.md). Only settings that code uses are read.

Settings of later stages (deploy, backups) are not read yet, so nobody mistakes them for
working configuration. Secrets are never echoed back.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Literal, cast

from xboct.domain.errors import DomainError
from xboct.domain.timeutil import get_zone

AppEnv = Literal["test", "demo", "production"]
LlmProvider = Literal["fixture", "openai"]

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/xboct.db"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    app_env: AppEnv
    database_url: str
    user_timezone: str
    llm_provider: LlmProvider


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    app_env = env.get("APP_ENV", "demo")
    if app_env not in ("test", "demo", "production"):
        raise ConfigError("APP_ENV must be test, demo or production")
    database_url = env.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if not database_url.startswith("sqlite+aiosqlite:///"):
        raise ConfigError("DATABASE_URL must be sqlite+aiosqlite:///path")
    timezone = env.get("USER_TIMEZONE", "Asia/Krasnoyarsk")
    try:
        get_zone(timezone)
    except DomainError as exc:
        raise ConfigError(f"USER_TIMEZONE: {exc.message}") from exc
    llm_provider = env.get("LLM_PROVIDER", "fixture")
    if llm_provider not in ("fixture", "openai"):
        raise ConfigError("LLM_PROVIDER must be fixture or openai")
    if app_env == "production" and not database_url.startswith("sqlite+aiosqlite:////"):
        raise ConfigError(
            "production DATABASE_URL must be an absolute path (sqlite+aiosqlite:////data/xboct.db)"
        )
    return Config(
        app_env=cast(AppEnv, app_env),
        database_url=database_url,
        user_timezone=timezone,
        llm_provider=cast(LlmProvider, llm_provider),
    )


@dataclass(frozen=True)
class BotConfig:
    owner_id: int
    token: str = field(repr=False)  # never printed or logged


def load_bot_config(env: Mapping[str, str] | None = None) -> BotConfig:
    """Telegram settings for `xboct run`. Fails closed; the token value is never echoed."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    owner = env.get("TELEGRAM_OWNER_ID", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
    if ":" not in token:
        raise ConfigError("TELEGRAM_BOT_TOKEN does not look like a bot token")
    if not owner.isdigit() or int(owner) <= 0:
        raise ConfigError("TELEGRAM_OWNER_ID must be the owner's numeric Telegram user id")
    return BotConfig(owner_id=int(owner), token=token)


@dataclass(frozen=True)
class LlmConfig:
    provider: LlmProvider
    model: str | None
    daily_call_limit: int
    api_key: str | None = field(default=None, repr=False)  # never printed or logged
    reasoning_effort: str | None = None


def load_llm_config(env: Mapping[str, str] | None = None) -> LlmConfig:
    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "fixture")
    if provider not in ("fixture", "openai"):
        raise ConfigError("LLM_PROVIDER must be fixture or openai")
    limit_text = env.get("LLM_DAILY_CALL_LIMIT", "50").strip()
    if not limit_text.isdigit():
        raise ConfigError("LLM_DAILY_CALL_LIMIT must be a non-negative integer")
    if provider == "fixture":
        return LlmConfig("fixture", None, int(limit_text))
    key = env.get("OPENAI_API_KEY", "").strip()
    model = env.get("OPENAI_MODEL", "").strip()
    if not key:
        raise ConfigError("OPENAI_API_KEY is not set (LLM_PROVIDER=openai)")
    if not model:
        raise ConfigError("OPENAI_MODEL is not set: choose the model explicitly")
    effort = env.get("OPENAI_REASONING_EFFORT", "").strip() or None
    allowed = ("none", "minimal", "low", "medium", "high", "xhigh")
    if effort is not None and effort not in allowed:
        raise ConfigError(f"OPENAI_REASONING_EFFORT must be one of {', '.join(allowed)}")
    return LlmConfig("openai", model, int(limit_text), api_key=key, reasoning_effort=effort)


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    session_days: int
    secure_cookies: bool
    public_origin: str | None  # scheme://host[:port] seen by the browser (behind a proxy)
    trusted_proxies: str = "127.0.0.1"  # whose X-Forwarded-For is believed (uvicorn)


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def load_web_config(env: Mapping[str, str] | None = None) -> WebConfig:
    """Web settings for `xboct serve`. Cookies are Secure unless explicitly local."""
    env = os.environ if env is None else env
    host = env.get("WEB_HOST", "127.0.0.1").strip()
    port_text = env.get("WEB_PORT", "8000").strip()
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ConfigError("WEB_PORT must be a TCP port number")
    days_text = env.get("WEB_SESSION_DAYS", "7").strip()
    if not days_text.isdigit() or not 1 <= int(days_text) <= 30:
        raise ConfigError("WEB_SESSION_DAYS must be 1..30")
    secure_text = env.get("WEB_SECURE_COOKIES", "true").strip().lower()
    if secure_text not in ("true", "false"):
        raise ConfigError("WEB_SECURE_COOKIES must be true or false")
    secure = secure_text == "true"
    if not secure and host not in LOCAL_HOSTS:
        raise ConfigError("WEB_SECURE_COOKIES=false is allowed only with a localhost WEB_HOST")
    origin = env.get("WEB_PUBLIC_ORIGIN", "").strip().rstrip("/") or None
    if origin is not None and not origin.startswith(("https://", "http://")):
        raise ConfigError("WEB_PUBLIC_ORIGIN must look like https://host")
    if origin is not None and origin.startswith("http://") and secure:
        raise ConfigError("WEB_PUBLIC_ORIGIN over http needs WEB_SECURE_COOKIES=false (local only)")
    proxies = env.get("WEB_TRUSTED_PROXIES", "127.0.0.1").strip() or "127.0.0.1"
    return WebConfig(host, int(port_text), int(days_text), secure, origin, proxies)


@dataclass(frozen=True)
class BackupConfig:
    directory: Path | None
    daily_at: time


def load_backup_config(env: Mapping[str, str] | None = None) -> BackupConfig:
    env = os.environ if env is None else env
    directory = env.get("BACKUP_DIR", "").strip()
    at_text = env.get("BACKUP_TIME", "04:00").strip()
    try:
        daily_at = time.fromisoformat(at_text)
    except ValueError:
        raise ConfigError("BACKUP_TIME must look like 04:00") from None
    path = Path(directory) if directory else None
    if path is not None and not path.is_absolute():
        raise ConfigError("BACKUP_DIR must be an absolute path")
    return BackupConfig(path, daily_at)


def check_production(config: Config, web: WebConfig, backup: BackupConfig) -> None:
    """Production fails closed: HTTPS origin, Secure cookies, backups configured."""
    if config.app_env != "production":
        return
    if not (web.public_origin or "").startswith("https://"):
        raise ConfigError("production needs WEB_PUBLIC_ORIGIN=https://<domain>")
    if not web.secure_cookies:
        raise ConfigError("production needs WEB_SECURE_COOKIES=true")
    if backup.directory is None:
        raise ConfigError("production needs BACKUP_DIR (daily snapshots)")
