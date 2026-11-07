"""The owner's chat menu button: «Открыть приложение» → the Mini App, or back to commands.

Run explicitly (`xboct telegram-setup`), never from the application's startup. The button
is set for the owner's private chat only; it is only a shortcut to <origin>/tma, the sign-in
is checked by the server (docs/MINI_APP.md).
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlsplit

BUTTON_TEXT = "Открыть приложение"


class MenuError(Exception):
    pass


class MenuClient(Protocol):
    async def set_menu(self, chat_id: int, url: str | None) -> None:
        """url set: a web-app button with BUTTON_TEXT; None: the ordinary commands menu."""
        ...

    async def close(self) -> None: ...


def mini_app_url(origin: str | None) -> str:
    """Telegram opens Mini Apps over HTTPS only; a local or missing origin is refused."""
    if not origin:
        raise MenuError("WEB_PUBLIC_ORIGIN is not set")
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
        raise MenuError("WEB_PUBLIC_ORIGIN must be an https origin like https://host")
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        raise MenuError("WEB_PUBLIC_ORIGIN must be the public https address, not localhost")
    return f"https://{parts.netloc}/tma"


async def apply_menu(client: MenuClient, owner_id: int, origin: str | None, *, reset: bool) -> str:
    """Returns a one-line description of what was set."""
    if reset:
        await client.set_menu(owner_id, None)
        return f"chat {owner_id}: menu button is the ordinary commands menu again"
    url = mini_app_url(origin)
    await client.set_menu(owner_id, url)
    return f"chat {owner_id}: menu button «{BUTTON_TEXT}» opens {url}"


class AiogramMenuClient:
    def __init__(self, token: str) -> None:
        from aiogram import Bot

        self._bot = Bot(token=token)

    async def set_menu(self, chat_id: int, url: str | None) -> None:
        from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

        button = (
            MenuButtonCommands()
            if url is None
            else MenuButtonWebApp(text=BUTTON_TEXT, web_app=WebAppInfo(url=url))
        )
        try:
            await self._bot.set_chat_menu_button(chat_id=chat_id, menu_button=button)
        except Exception as exc:  # the token and response text are not echoed
            raise MenuError(f"Telegram refused the menu change ({type(exc).__name__})") from exc

    async def close(self) -> None:
        await self._bot.session.close()
