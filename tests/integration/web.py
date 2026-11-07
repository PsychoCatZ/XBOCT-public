"""Web test driver: a real httpx client against the ASGI app on the test database.

Forms are submitted from what the server actually rendered (hidden csrf/idem/version),
so the tests exercise the same HTML the owner's browser gets.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx

from tests.conftest import Harness
from xboct.web.app import WebOptions, create_app
from xboct.web.auth import AuthService
from xboct.web.tma import TmaConfig

BASE = "http://testserver"


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._current = {"action": a.get("action"), "method": a.get("method"), "fields": []}
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None and a.get("type") == "hidden":
            self._current["fields"].append((a.get("name"), a.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def forms_in(html: str) -> list[dict[str, Any]]:
    parser = _Forms()
    parser.feed(html)
    return parser.forms


def hidden(html: str, action: str, index: int = 0, **match: str) -> dict[str, str]:
    """Hidden fields of the index-th form posting to `action` (optionally by field values)."""
    found = []
    for form in forms_in(html):
        if form["action"] != action:
            continue
        fields = dict(form["fields"])
        if all(fields.get(k) == v for k, v in match.items()):
            found.append(fields)
    assert len(found) > index, f"no form for {action} {match} in page"
    return found[index]


class Web:
    def __init__(
        self, x: Harness, *, secure_cookies: bool = False, tma: TmaConfig | None = None
    ) -> None:
        self.x = x
        self.auth = AuthService(x.app)
        self.app = create_app(x.app, self.auth, WebOptions(secure_cookies=secure_cookies, tma=tma))
        base = BASE.replace("http:", "https:") if secure_cookies else BASE
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("203.0.113.5", 50000)),
            base_url=base,
            headers={"origin": base},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(path, **kw)

    async def post(self, path: str, data: dict[str, Any], **kw: Any) -> httpx.Response:
        return await self.client.post(path, data=data, **kw)

    async def login(self) -> httpx.Response:
        code = await self.auth.issue_code()
        return await self.submit_code(code)

    async def submit_code(self, code: str) -> httpx.Response:
        page = await self.get("/login")
        token = hidden(page.text, "/login")["login_csrf"]
        return await self.post("/login", {"code": code, "login_csrf": token})

    async def page(self, path: str) -> str:
        response = await self.get(path)
        assert response.status_code == 200, (path, response.status_code)
        return response.text

    async def submit(
        self, page_path: str, action: str, extra: dict[str, Any] | None = None, **match: str
    ) -> httpx.Response:
        """Load a page, take the matching form's hidden fields, add `extra`, post it."""
        html = await self.page(page_path)
        data: dict[str, Any] = {**hidden(html, action, **match), **(extra or {})}
        return await self.post(action, data)

    async def follow(self, response: httpx.Response) -> str:
        assert response.status_code == 303, (response.status_code, response.text[:500])
        return await self.page(response.headers["location"])


def text_of(html: str) -> str:
    """Visible-ish text without tags, for message assertions."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))
