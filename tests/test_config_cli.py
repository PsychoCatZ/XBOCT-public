import pytest

from xboct.cli import main
from xboct.config import ConfigError, load_bot_config, load_config, load_llm_config
from xboct.storage import migrate


def test_defaults_without_secrets():
    config = load_config({})
    assert config.app_env == "demo"
    assert config.user_timezone == "Asia/Krasnoyarsk"
    assert config.llm_provider == "fixture"


@pytest.mark.parametrize(
    "env",
    [
        {"APP_ENV": "prod"},
        {"APP_ENV": "production"},
        {"DATABASE_URL": "postgresql://x"},
        {"USER_TIMEZONE": "Nowhere/City"},
    ],
)
def test_invalid_or_premature_config_fails_closed(env):
    with pytest.raises(ConfigError):
        load_config(env)


def test_demo_runs_offline_on_fresh_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    db = tmp_path / "demo.db"
    assert main(["demo", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "15 октября" in out
    assert "replayed=True" in out
    assert "balance_low" in out
    assert main(["demo", "--db", str(db)]) == 2  # never overwrites an existing database


def test_migrate_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'm' / 'x.db'}")
    assert main(["migrate"]) == 0
    assert f"schema at {migrate.head_revision()}" in capsys.readouterr().out


def test_bot_config_fails_closed_and_hides_token():
    with pytest.raises(ConfigError):
        load_bot_config({})
    with pytest.raises(ConfigError):
        load_bot_config({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_OWNER_ID": "me"})
    config = load_bot_config({"TELEGRAM_BOT_TOKEN": "123:secret-value", "TELEGRAM_OWNER_ID": "42"})
    assert config.owner_id == 42
    assert "secret-value" not in repr(config)


def test_run_refuses_without_token(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["run"]) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err


def test_llm_config_openai_requires_key_and_model_and_hides_key():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_llm_config({"LLM_PROVIDER": "openai", "OPENAI_MODEL": "m"})
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        load_llm_config({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"})
    config = load_llm_config(
        {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-secret",
            "OPENAI_MODEL": "some-model",
            "LLM_DAILY_CALL_LIMIT": "7",
        }
    )
    assert (config.provider, config.model, config.daily_call_limit) == ("openai", "some-model", 7)
    assert "sk-secret" not in repr(config)
    assert load_llm_config({}).provider == "fixture"
    with pytest.raises(ConfigError):
        load_llm_config({"LLM_DAILY_CALL_LIMIT": "-1"})


def test_reasoning_effort_config():
    base = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x", "OPENAI_MODEL": "gpt-6-luna"}
    assert load_llm_config(base).reasoning_effort is None
    assert load_llm_config({**base, "OPENAI_REASONING_EFFORT": "none"}).reasoning_effort == "none"
    with pytest.raises(ConfigError, match="OPENAI_REASONING_EFFORT"):
        load_llm_config({**base, "OPENAI_REASONING_EFFORT": "max"})


def test_web_config_defaults_are_safe():
    from xboct.config import load_web_config

    web = load_web_config({})
    assert (web.host, web.port, web.session_days, web.secure_cookies) == (
        "127.0.0.1",
        8000,
        7,
        True,
    )
    assert web.public_origin is None
    local = load_web_config({"WEB_SECURE_COOKIES": "false"})
    assert local.secure_cookies is False
    proxied = load_web_config(
        {"WEB_HOST": "0.0.0.0", "WEB_PUBLIC_ORIGIN": "https://xboct.example/"}
    )
    assert proxied.public_origin == "https://xboct.example"


@pytest.mark.parametrize(
    "env",
    [
        {"WEB_HOST": "0.0.0.0", "WEB_SECURE_COOKIES": "false"},  # insecure cookies in public
        {"WEB_PORT": "http"},
        {"WEB_SESSION_DAYS": "365"},
        {"WEB_PUBLIC_ORIGIN": "xboct.example"},
        {"WEB_PUBLIC_ORIGIN": "http://xboct.example"},  # plain http with Secure cookies
    ],
)
def test_web_config_fails_closed(env):
    from xboct.config import load_web_config

    with pytest.raises(ConfigError):
        load_web_config(env)


def test_serve_refuses_without_bot_settings_or_unsafe_demo(monkeypatch, capsys):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "WEB_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert main(["serve"]) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    assert main(["serve", "--no-bot", "--demo-login"]) == 2
    assert "--demo-login needs" in capsys.readouterr().err


PROD = {
    "APP_ENV": "production",
    "DATABASE_URL": "sqlite+aiosqlite:////data/xboct.db",
    "WEB_HOST": "0.0.0.0",
    "WEB_PUBLIC_ORIGIN": "https://xboct.example",
    "BACKUP_DIR": "/backups",
}


def test_production_config_is_accepted_when_complete():
    from xboct.config import check_production, load_backup_config, load_web_config

    config = load_config(PROD)
    check_production(config, load_web_config(PROD), load_backup_config(PROD))
    assert config.app_env == "production"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"DATABASE_URL": "sqlite+aiosqlite:///./data/xboct.db"}, "absolute path"),
        ({"WEB_PUBLIC_ORIGIN": ""}, "WEB_PUBLIC_ORIGIN=https"),
        ({"BACKUP_DIR": ""}, "BACKUP_DIR"),
        ({"BACKUP_DIR": "backups"}, "absolute"),
        ({"BACKUP_TIME": "4 утра"}, "BACKUP_TIME"),
    ],
)
def test_production_fails_closed(change, message):
    from xboct.config import check_production, load_backup_config, load_web_config

    env = {**PROD, **change}
    with pytest.raises(ConfigError, match=message):
        check_production(load_config(env), load_web_config(env), load_backup_config(env))


def test_production_refuses_web_only_and_demo_login(monkeypatch, capsys):
    for key, value in PROD.items():
        monkeypatch.setenv(key, value)
    assert main(["serve", "--no-bot"]) == 2
    assert "--no-bot is not allowed in production" in capsys.readouterr().err
    assert main(["serve", "--demo-login"]) == 2
    assert "--demo-login needs" in capsys.readouterr().err
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["serve"]) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err
