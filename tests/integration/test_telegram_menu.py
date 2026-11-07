"""`xboct telegram-setup`: the owner's menu button for the Mini App (no real Telegram call)."""

from __future__ import annotations

import pytest

from xboct.cli import main
from xboct.telegram.menu import BUTTON_TEXT, MenuError, apply_menu, mini_app_url


class FakeMenu:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    async def set_menu(self, chat_id: int, url: str | None) -> None:
        self.calls.append((chat_id, url))

    async def close(self) -> None:
        return None


async def test_the_button_opens_the_public_tma_url_in_the_owner_chat():
    client = FakeMenu()
    text = await apply_menu(client, 42, "https://xboct.example", reset=False)
    assert client.calls == [(42, "https://xboct.example/tma")]
    assert BUTTON_TEXT == "Открыть приложение"
    assert "https://xboct.example/tma" in text


async def test_reset_restores_the_ordinary_commands_menu():
    client = FakeMenu()
    await apply_menu(client, 42, None, reset=True)  # no origin needed to go back
    assert client.calls == [(42, None)]


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "http://xboct.example",
        "https://localhost:8000",
        "https://127.0.0.1",
        "xboct.example",
    ],
)
async def test_unusable_origins_are_refused_before_any_call(origin):
    client = FakeMenu()
    with pytest.raises(MenuError):
        await apply_menu(client, 42, origin, reset=False)
    assert client.calls == []


def test_a_path_in_the_origin_is_refused():
    with pytest.raises(MenuError):
        mini_app_url("https://xboct.example/app")
    assert mini_app_url("https://xboct.example/") == "https://xboct.example/tma"


def env(monkeypatch, **values: str) -> None:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "WEB_PUBLIC_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_dry_run_sends_nothing_and_does_not_print_the_token(monkeypatch, capsys):
    env(
        monkeypatch,
        TELEGRAM_BOT_TOKEN="123456:SECRET-TOKEN",
        TELEGRAM_OWNER_ID="42",
        WEB_PUBLIC_ORIGIN="https://xboct.example",
    )
    assert main(["telegram-setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "https://xboct.example/tma" in out
    assert "chat 42" in out
    assert "SECRET-TOKEN" not in out
    assert main(["telegram-setup", "--dry-run", "--reset"]) == 0
    assert "ordinary commands menu" in capsys.readouterr().out


def test_dry_run_refuses_a_missing_or_local_origin(monkeypatch, capsys):
    env(monkeypatch, TELEGRAM_BOT_TOKEN="123456:SECRET-TOKEN", TELEGRAM_OWNER_ID="42")
    assert main(["telegram-setup", "--dry-run"]) == 1
    assert "WEB_PUBLIC_ORIGIN" in capsys.readouterr().err


def test_without_the_bot_config_the_command_fails_closed(monkeypatch, capsys):
    env(monkeypatch)
    assert main(["telegram-setup", "--dry-run"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
