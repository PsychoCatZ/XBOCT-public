"""Browser QA of the Telegram Mini App sign-in and pages, on fictitious data.

Real Chromium, real server (fresh temporary database), no network and no Telegram: the
parent «Telegram Web» page (https://web.telegram.org) and the SDK script
(https://telegram.org/js/telegram-web-app.js) are served by this script through request
interception; the app is loaded in a cross-site iframe from http://localhost:<port>. initData
is signed here with a synthetic bot token by an independent HMAC implementation.

Checks: sign-in and cookie retention in the iframe, navigation and POST forms inside it,
back button, theme variables, bottom bar vs content and keyboard, expired session without
redirect loops, stale initData, blocked cookies, no-Telegram access, headers and framing
of the ordinary web. Screenshots go to qa-artifacts/tma/ (not committed). Not a client
test: Android, iOS, Desktop and the real Telegram Web are checked by hand (docs/MINI_APP.md).

    uv sync --frozen --group dev --group qa
    uv run python scripts/tma_qa.py [--chromium /path/to/chrome] [--third-party-cookie-phaseout]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import socket
import subprocess
import sys
import tempfile
import time as clock_time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import uvicorn
from playwright.async_api import BrowserContext, Frame, Page, Route, async_playwright

from xboct.application import commands as c
from xboct.application.context import CommandContext
from xboct.application.service import Application
from xboct.domain.clock import SystemClock
from xboct.storage import migrate
from xboct.storage.db import sqlite_url
from xboct.web import auth as auth_module
from xboct.web.app import WebOptions, create_app
from xboct.web.auth import AuthService
from xboct.web.tma import TmaConfig

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qa-artifacts" / "tma"
# Chromium's simulation of the third-party cookie phase-out: only Partitioned cookies survive.
PHASEOUT_ARGS = ["--test-third-party-cookie-phaseout"]
DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
BOT_TOKEN = "123456:SYNTHETIC-QA-TOKEN-NOT-REAL"
OWNER_ID = 424242
PARENT_URL = "https://web.telegram.org/a/"
SDK_URL = "https://telegram.org/js/telegram-web-app.js"
# The imitated Telegram Web page is «public» and the app listens on loopback, which Chromium's
# local-network checks would refuse; a real deployment is a public host, so they are off here.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-features=PrivateNetworkAccessForNavigations,PrivateNetworkAccessForWorkers,"
    "PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,"
    "BlockInsecurePrivateNetworkRequests,LocalNetworkAccessChecks",
]

THEMES = {
    "light": {
        "bg_color": "#ffffff",
        "secondary_bg_color": "#efeff4",
        "text_color": "#000000",
        "hint_color": "#6d6d72",
        "button_color": "#2481cc",
        "button_text_color": "#ffffff",
        "destructive_text_color": "#d0342c",
    },
    "dark": {
        "bg_color": "#212121",
        "secondary_bg_color": "#0f0f0f",
        "text_color": "#ffffff",
        "hint_color": "#aaaaaa",
        "button_color": "#8774e1",
        "button_text_color": "#ffffff",
        "destructive_text_color": "#ff595a",
    },
}

SDK_TEMPLATE = """
(function () {
  var calls = [];
  var handlers = {};
  var backClick = null;
  window.__tg = { calls: calls, handlers: handlers, back: function () { if (backClick) backClick(); } };
  function note(name, arg) { calls.push([name, arg === undefined ? null : arg]); }
  var HOME = __HOME__;  // status shown by checkHomeScreenStatus (null: never answers)
  var added = false;
  var checks = 0;
  window.Telegram = { WebApp: {
    initData: __INIT_DATA__,
    initDataUnsafe: { user: { id: 999 } },  // a lie on purpose: the server must ignore it
    themeParams: __THEME__, colorScheme: "__SCHEME__", version: "__VERSION__",
    safeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    contentSafeAreaInset: { top: 0, bottom: 0, left: 0, right: 0 },
    isVersionAtLeast: function (v) { return parseFloat(v) <= __VERSION__; },
    ready: function () { note("ready"); }, expand: function () { note("expand"); },
    close: function () { note("close"); },
    setHeaderColor: function (v) { note("setHeaderColor", v); },
    setBackgroundColor: function (v) { note("setBackgroundColor", v); },
    openLink: function (u) { note("openLink", u); },
    showConfirm: function (q, cb) { note("showConfirm", q); setTimeout(function () { cb(true); }, 0); },
    onEvent: function (n, f) { handlers[n] = f; },
    __HOME_METHODS__
    BackButton: {
      show: function () { note("back.show"); }, hide: function () { note("back.hide"); },
      onClick: function (f) { backClick = f; }
    }
  } };
})();
"""

HOME_METHODS = """
    checkHomeScreenStatus: function (cb) {
      var n = checks++;
      note("checkHomeScreenStatus");
      var status = HOME.status;
      if (HOME.sequence) { status = HOME.sequence[Math.min(n, HOME.sequence.length - 1)]; }
      else if (added && HOME.after) { status = HOME.after; }
      var delay = (HOME.delays || [])[n] || 0;
      if (status !== null) { setTimeout(function () { cb(status); }, delay); }
    },
    addToHomeScreen: function () {
      note("addToHomeScreen");
      if (HOME.throws) { throw new Error("simulated failure"); }
      added = true;
      if (HOME.event && handlers.homeScreenAdded) { handlers.homeScreenAdded(); }
    },
"""

PARENT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Web (imitation)</title>
<style>html,body{margin:0;height:100%;background:#999}iframe{border:0;width:100%;height:100%}</style>
</head><body><iframe src="__SRC__"></iframe></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """Throw-away self-signed certificate for localhost (the page under test is https)."""
    key, cert = directory / "key.pem", directory / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return key, cert


def init_data(auth_date: int | None = None, user_id: int = OWNER_ID) -> str:
    """initData as Telegram builds it (signature field included in the signed text)."""
    fields = {
        "query_id": "AAHsynthetic",
        "user": json.dumps({"id": user_id, "first_name": "Тест", "language_code": "ru"}),
        "auth_date": str(auth_date if auth_date is not None else int(clock_time.time())),
        "signature": "c3ludGhldGlj",
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Seeder:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.n = 0

    async def run(self, command: c.Command) -> dict:
        self.n += 1
        result = await self.app.execute(
            command, CommandContext(f"qa:{self.n}", "test", self.app.clock.now())
        )
        assert result.ok, result.message
        return result.data


async def seed(s: Seeder, today: date) -> dict[str, str]:
    ids: dict[str, str] = {}
    ids["overdue"] = (
        await s.run(
            c.CreateTask(title="Оплатить штраф за парковку", due_date=today - timedelta(days=2))
        )
    )["task_id"]
    ids["today"] = (await s.run(c.CreateTask(title="Позвонить маме", due_date=today)))["task_id"]
    await s.run(
        c.CreateTask(
            title="Записаться к стоматологу на профилактический осмотр и чистку зубов",
            due_date=today + timedelta(days=3),
            note="Заметка — просто текст.\n" * 6,
            url="https://example.com/some/long/path/for/the/external/link/check",
        )
    )
    await s.run(c.CreateTask(title="Разобрать полку"))
    await s.run(
        c.CreateObligation(
            title="VPS Германия",
            kind="balance",
            balance=c.BalanceStart(
                checkpoint_minor=20_000,
                checkpoint_at=s.app.clock.now(),
                rate_minor_per_day=4_000,
            ),
        )
    )
    await s.run(
        c.CreateObligation(
            title="Подписка «Музыка»", kind="coverage", coverage_until=today + timedelta(days=34)
        )
    )
    await s.run(
        c.CreateObligation(
            title="Аренда",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=today + timedelta(days=5)),
        )
    )
    return ids


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, condition: bool, label: str, detail: str = "") -> None:
        print(
            f"{'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail and not condition else ''}"
        )
        if not condition:
            self.failures.append(label)


class Harness:
    def __init__(self, base: str) -> None:
        self.base = base
        self.state = {
            "init_data": init_data(),
            "scheme": "light",
            "strip_cookies": False,
            "version": 8.0,
            "home": {"methods": True, "status": "missed", "after": None, "event": False},
        }
        self.console: list[str] = []
        self.navigations = 0

    async def install(self, context: BrowserContext, src: str) -> None:
        async def parent(route: Route) -> None:
            await route.fulfill(
                status=200, content_type="text/html", body=PARENT_HTML.replace("__SRC__", src)
            )

        async def sdk(route: Route) -> None:
            body = (
                SDK_TEMPLATE.replace("__INIT_DATA__", json.dumps(self.state["init_data"]))
                .replace("__THEME__", json.dumps(THEMES[self.state["scheme"]]))
                .replace("__SCHEME__", self.state["scheme"])
                .replace("__VERSION__", str(self.state["version"]))
                .replace("__HOME_METHODS__", HOME_METHODS if self.state["home"]["methods"] else "")
                .replace("__HOME__", json.dumps(self.state["home"]))
            )
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                headers={"cache-control": "no-store"},
                body=body,
            )

        async def auth(route: Route) -> None:
            if self.state["strip_cookies"]:
                # A client that accepts the answer but keeps no cookie: the server is not
                # reached (route.fetch would store the cookie in the context itself).
                await route.fulfill(status=200, json={"ok": True})
            else:
                await route.fallback()

        await context.route(PARENT_URL, parent)
        await context.route(SDK_URL, sdk)
        await context.route(f"{self.base}/tma/auth", auth)

    def watch(self, page: Page) -> None:
        page.on(
            "console",
            lambda m: self.console.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )
        page.on("framenavigated", lambda f: self._nav(f))

    def _nav(self, frame: Frame) -> None:
        if frame.url.startswith(self.base + "/tma"):
            self.navigations += 1


def child(page: Page) -> Frame:
    frames = page.frames
    assert len(frames) > 1, "no iframe"
    return frames[1]


async def wait_path(page: Page, path: str, wait_ms: int = 8000) -> None:
    """The iframe is cross-site: its location is read through Playwright, not from the parent."""
    deadline = clock_time.monotonic() + wait_ms / 1000
    while clock_time.monotonic() < deadline:
        if len(page.frames) > 1 and urlsplit(page.frames[1].url).path == path:
            return
        await page.wait_for_timeout(100)
    at = page.frames[1].url if len(page.frames) > 1 else "no frame"
    raise TimeoutError(f"iframe did not reach {path}; at {at}")


async def browser_cookies(context: BrowserContext, page: Page) -> list[dict]:
    """Every cookie of the browser with its partition key (Playwright's list hides it)."""
    cdp = await context.new_cdp_session(page)
    try:
        return (await cdp.send("Network.getAllCookies"))["cookies"]
    finally:
        await cdp.detach()


async def settle(page: Page) -> None:
    await page.wait_for_timeout(400)


async def open_app(context: BrowserContext, harness: Harness, report: Report) -> Page:
    page = await context.new_page()
    harness.watch(page)
    await page.goto(PARENT_URL, wait_until="commit")
    return page


async def main(chromium: str | None, phaseout: bool = False) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    workdir = Path(tempfile.mkdtemp(prefix="xboct-tma-qa-"))
    url = sqlite_url(workdir / "qa.db")
    migrate.upgrade(url)
    core = await Application.open(url, SystemClock())
    # This run opens more sessions than the window allows; the limit has its own tests.
    auth_module.MAX_SESSIONS_PER_WINDOW = 10_000
    auth = AuthService(core)
    port = free_port()
    base = f"https://localhost:{port}"
    app = create_app(
        core,
        auth,
        WebOptions(secure_cookies=True, public_origin=base, tma=TmaConfig(BOT_TOKEN, OWNER_ID)),
    )
    key, cert = make_certificate(workdir)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            ssl_keyfile=str(key),
            ssl_certfile=str(cert),
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.05)
    today = await core.read(lambda uow: _today(uow))
    ids = await seed(Seeder(core), today)
    report = Report()
    harness = Harness(base)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=chromium or DEFAULT_CHROMIUM,
            args=[*CHROMIUM_ARGS, *(PHASEOUT_ARGS if phaseout else [])],
        )

        def new_context(width: int = 390, height: int = 844):
            return browser.new_context(
                viewport={"width": width, "height": height},
                ignore_https_errors=True,
                is_mobile=width < 600,
                has_touch=width < 600,
            )

        # --- 1. sign-in chain and cookie retention in the cross-site iframe ------------------
        context = await new_context()
        await harness.install(context, f"{base}/tma")
        page = await open_app(context, harness, report)
        await wait_path(page, "/")
        frame = child(page)
        report.ok(
            await frame.evaluate("document.body.className") == "tma",
            "iframe: signed in, Mini App page /",
        )
        text = await frame.inner_text("body")
        report.ok(
            "Требует внимания" in text and "VPS Германия" in text,
            "home screen rendered in the iframe",
        )
        cookies = {k["name"]: k for k in await browser_cookies(context, page)}
        tma = cookies.get("__Host-xboct_tma")
        report.ok(tma is not None, "cookie __Host-xboct_tma stored in the iframe context")
        if tma:
            report.ok(
                tma["secure"]
                and tma["httpOnly"]
                and tma["sameSite"] == "None"
                and tma["path"] == "/"
                and not tma["session"],
                "cookie attributes Secure; HttpOnly; SameSite=None; Path=/",
                json.dumps({k: tma.get(k) for k in ("secure", "httpOnly", "sameSite", "path")}),
            )
            report.ok(
                bool(tma.get("partitionKey")),
                "cookie is partitioned",
                json.dumps(tma.get("partitionKey")),
            )
        report.ok("xboct_session" not in cookies, "the ordinary session cookie was not set")

        calls = await frame.evaluate("window.__tg.calls")
        names = [n for n, _ in calls]
        report.ok("ready" in names and "expand" in names, "SDK: ready() and expand() called")
        report.ok(["back.hide", None] in calls, "back button hidden on the home screen")
        report.ok(
            await frame.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--card').trim()"
            )
            == "#ffffff",
            "theme variables applied (light)",
        )
        await page.screenshot(path=OUT / "phone-home.png", full_page=False)

        # --- 2. navigation, card, POST form, redirect, back ---------------------------------
        await frame.click(".bottom-nav a[href='/payments']")
        await wait_path(page, "/payments")
        frame = child(page)
        report.ok(
            ["back.show", None] in await frame.evaluate("window.__tg.calls"),
            "back button shown on an inner page",
        )
        report.ok(
            "VPS Германия" in await frame.inner_text("body"),
            "payments page opens inside the iframe",
        )
        report.ok(
            await frame.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--card').trim()"
            )
            == "#ffffff"
            and "ready" in [n for n, _ in await frame.evaluate("window.__tg.calls")],
            "SDK ready and theme applied again after a server navigation",
        )
        await frame.goto(f"{base}/tasks/{ids['overdue']}")
        await settle(page)
        frame = child(page)
        await frame.click(
            "form[action$='/complete'] button"
        )  # existing form: CSRF + origin + version
        await wait_path(page, f"/tasks/{ids['overdue']}")
        await settle(page)
        frame = child(page)
        body = await frame.inner_text("body")
        report.ok(
            "отмечено" in body.lower() or "готово" in body.lower() or "выполн" in body.lower(),
            "POST form works with the Mini App session (completed the task)",
            body[:200],
        )
        await frame.goto(f"{base}/")
        await settle(page)
        report.ok(
            "Оплатить штраф за парковку" not in await child(page).inner_text("body"),
            "the completed task left the home screen",
        )
        # back button handler
        await child(page).goto(f"{base}/payments")
        await settle(page)
        await child(page).evaluate("window.__tg.back()")
        await settle(page)
        report.ok(True, "back handler callable")

        # --- 3. bottom bar: does not cover the content; hidden with the keyboard -------------
        await child(page).goto(f"{base}/tasks/new")
        await settle(page)
        frame = child(page)
        geom = await frame.evaluate(
            """() => {
              window.scrollTo(0, document.body.scrollHeight);
              const nav = document.querySelector('.bottom-nav').getBoundingClientRect();
              const items = [...document.querySelectorAll('main button, main a, main input, main textarea')];
              const last = items[items.length - 1].getBoundingClientRect();
              return {navTop: nav.top, lastBottom: last.bottom, vh: window.innerHeight, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth};
            }"""
        )
        report.ok(
            geom["lastBottom"] <= geom["navTop"] + 1,
            "bottom bar does not cover the last field/button",
            str(geom),
        )
        report.ok(geom["sw"] <= geom["cw"], "no horizontal scroll (phone)", str(geom))
        await frame.focus("input[name=title]")
        report.ok(
            await frame.evaluate("document.body.classList.contains('kb')"),
            "keyboard class set on focus",
        )
        report.ok(
            await frame.evaluate("getComputedStyle(document.querySelector('.bottom-nav')).display")
            == "none",
            "bottom bar hidden while typing",
        )
        await frame.evaluate("document.activeElement.blur()")
        await settle(page)
        report.ok(
            await frame.evaluate("getComputedStyle(document.querySelector('.bottom-nav')).display")
            != "none",
            "bottom bar back after blur",
        )

        # --- 4. external link goes through Telegram -----------------------------------------
        await frame.goto(f"{base}/")
        await settle(page)
        card = await core.read(lambda uow: _first_note_task(uow))
        await child(page).goto(f"{base}/tasks/{card}")
        await settle(page)
        link = await child(page).query_selector("a[href^='https://example.com']")
        if link:
            await link.click()
            report.ok(
                any(n == "openLink" for n, _ in await child(page).evaluate("window.__tg.calls")),
                "external link opens via Telegram.openLink",
            )
        else:
            report.ok(False, "external link present on the card")

        # --- 5. expired session: re-enter through /tma, no loop ------------------------------
        await auth.revoke_all()
        harness.navigations = 0
        await child(page).goto(f"{base}/payments")
        await wait_path(page, "/payments")
        await settle(page)
        report.ok(
            "VPS Германия" in await child(page).inner_text("body"),
            "revoked session: back to the page after silent re-sign-in",
        )
        report.ok(
            harness.navigations <= 3,
            "no redirect loop after revoke",
            f"navigations={harness.navigations}",
        )

        # --- 6. logout in the Mini App ---------------------------------------------------------
        await child(page).goto(f"{base}/settings")
        await settle(page)
        await child(page).click("footer button:has-text('Выйти')")
        await settle(page)
        frame = child(page)
        report.ok(
            "Сессия завершена" in await frame.inner_text("body"),
            "logout: session-ended page, no automatic sign-in again",
        )
        report.ok(
            "__Host-xboct_tma" not in {k["name"] for k in await browser_cookies(context, page)},
            "logout deleted the partitioned cookie",
        )
        await context.close()

        report.ok(
            not harness.console,
            "no console errors / CSP violations (sign-in flows)",
            "; ".join(harness.console[:4]),
        )
        harness.console.clear()

        # --- 7. stale initData ------------------------------------------------------------------
        context = await new_context()
        harness.state["init_data"] = init_data(auth_date=int(clock_time.time()) - 3600)
        await harness.install(context, f"{base}/tma")
        page = await open_app(context, harness, report)
        await settle(page)
        await page.wait_for_timeout(600)
        frame = child(page)
        report.ok(
            "устарели" in await frame.inner_text("body"), "stale initData: asks to reopen the app"
        )
        report.ok(frame.url.endswith("/tma"), "stale initData: stays on /tma (no loop)", frame.url)
        await context.close()

        # --- 8. someone else's Telegram account ---------------------------------------------------
        context = await new_context()
        harness.state["init_data"] = init_data(user_id=OWNER_ID + 1)
        await harness.install(context, f"{base}/tma")
        page = await open_app(context, harness, report)
        await page.wait_for_timeout(800)
        report.ok(
            "Не удалось подтвердить" in await child(page).inner_text("body"),
            "another user: refused, stays on /tma",
        )
        await context.close()

        # --- 8b. reopen through /tma, then another Telegram account ---------------------------------
        context = await new_context()
        harness.state["init_data"] = init_data()
        await harness.install(context, f"{base}/tma")
        page = await open_app(context, harness, report)
        await wait_path(page, "/")
        first = {k["name"]: k["value"] for k in await browser_cookies(context, page)}.get(
            "__Host-xboct_tma"
        )
        await child(page).goto(f"{base}/tma")  # reopen with the same owner: checked again, rotated
        await wait_path(page, "/")
        second = {k["name"]: k["value"] for k in await browser_cookies(context, page)}.get(
            "__Host-xboct_tma"
        )
        report.ok(
            bool(first) and bool(second) and first != second,
            "reopen: identity checked again, session rotated",
        )
        harness.state["init_data"] = init_data(
            user_id=OWNER_ID + 1
        )  # another account, signed by Telegram
        await child(page).goto(f"{base}/tma")
        await page.wait_for_timeout(800)
        report.ok(
            "Не удалось подтвердить" in await child(page).inner_text("body"),
            "reopen as another user: refused",
        )
        report.ok(
            "__Host-xboct_tma" not in {k["name"] for k in await browser_cookies(context, page)},
            "reopen as another user: the previous session cookie is gone",
        )
        await child(page).goto(f"{base}/payments")
        await page.wait_for_timeout(800)
        report.ok(
            urlsplit(child(page).url).path == "/tma"
            and "VPS Германия" not in await child(page).inner_text("body"),
            "reopen as another user: the old session opens nothing",
            child(page).url,
        )
        harness.state["init_data"] = init_data()  # the owner again
        await child(page).goto(f"{base}/tma")
        await wait_path(page, "/")
        report.ok(True, "the owner signs in again after the other account")
        await context.close()

        # --- 9. cookies not kept by the client ---------------------------------------------------
        context = await new_context()
        harness.state["init_data"] = init_data()
        harness.state["strip_cookies"] = True
        harness.navigations = 0
        await harness.install(context, f"{base}/tma")
        page = await open_app(context, harness, report)
        await page.wait_for_timeout(1000)
        frame = child(page)
        report.ok(
            "не сохраняет cookies" in await frame.inner_text("body"),
            "blocked cookies: clear message with a way to the ordinary web",
        )
        report.ok(
            harness.navigations <= 2,
            "blocked cookies: no redirect loop",
            f"navigations={harness.navigations}",
        )
        link = frame.locator("a[href='/login'][data-external]:visible").first
        shown = await frame.locator("a[href='/login'][data-external]:visible").count()
        report.ok(shown > 0, "blocked cookies: link to the ordinary login is marked external")
        if shown:
            before = child(page).url
            await link.click()
            await page.wait_for_timeout(300)
            opened = [
                u for n, u in await child(page).evaluate("window.__tg.calls") if n == "openLink"
            ]
            report.ok(
                opened == [f"{base}/login"],
                "blocked cookies: the way out goes through Telegram openLink with an absolute URL",
                str(opened),
            )
            report.ok(
                child(page).url == before, "blocked cookies: the frame itself did not navigate"
            )
            outside = await context.new_page()  # what the system browser does with that URL
            response = await outside.goto(opened[0] if opened else f"{base}/login")
            report.ok(
                response.status == 200
                and await outside.query_selector("input[name=code]") is not None,
                "blocked cookies: the ordinary login works when opened outside the frame",
            )
        harness.state["strip_cookies"] = False
        await context.close()

        # --- 9b. the same dead end without the SDK: the link opens a new tab -------------------------
        context = await new_context()
        await harness.install(context, f"{base}/tma")
        await context.unroute(SDK_URL)
        await context.route(SDK_URL, lambda r: r.abort())
        page = await open_app(context, harness, report)
        await page.wait_for_timeout(800)
        frame = child(page)
        report.ok(
            "открывать из Telegram" in await frame.inner_text("body"),
            "no SDK in the iframe: explanation shown",
        )
        async with context.expect_page() as popup_info:
            await frame.click("a[href='/login'][data-external]:visible")
        popup = await popup_info.value
        await popup.wait_for_load_state()
        report.ok(
            popup.url == f"{base}/login"
            and await popup.query_selector("input[name=code]") is not None,
            "no SDK: the link opens the ordinary login in a new tab (not inside the frame)",
            popup.url,
        )
        report.ok(urlsplit(child(page).url).path == "/tma", "no SDK: the frame stays on /tma")
        await context.close()

        # Refusals above are answered 401/403 on purpose; the browser logs those as errors.
        harness.console.clear()

        # --- 10. /tma outside Telegram -------------------------------------------------------------
        context = await new_context()
        page = await context.new_page()
        await context.route(SDK_URL, lambda r: r.abort())
        await page.goto(f"{base}/tma")
        await page.wait_for_timeout(600)
        report.ok(
            "открывать из Telegram" in await page.inner_text("body"),
            "/tma without Telegram: explains, no access",
        )
        resp = await page.goto(f"{base}/")
        report.ok(
            page.url.endswith("/login"),
            "ordinary web without a session: redirected to /login (not /tma)",
            page.url,
        )
        report.ok(
            (resp.headers.get("x-frame-options") == "DENY")
            and "frame-ancestors 'none'" in resp.headers.get("content-security-policy", ""),
            "ordinary web keeps DENY / frame-ancestors 'none'",
        )
        await context.close()

        context = (
            await new_context()
        )  # top-level browser without Telegram: the link opens a new tab
        page = await context.new_page()
        await context.route(SDK_URL, lambda r: r.abort())
        await page.goto(f"{base}/tma")
        await page.wait_for_timeout(600)
        async with context.expect_page() as popup_info:
            await page.click("a[href='/login'][data-external]:visible")
        popup = await popup_info.value
        await popup.wait_for_load_state()
        report.ok(
            popup.url == f"{base}/login" and page.url.endswith("/tma"),
            "/tma without Telegram: the ordinary login opens in a new tab",
        )
        await context.close()

        # --- 11. the ordinary web cannot be framed by Telegram --------------------------------------
        context = await new_context(1100, 900)
        harness.state["init_data"] = init_data()
        await harness.install(context, f"{base}/login")
        page = await context.new_page()
        await page.goto(PARENT_URL)
        await page.wait_for_timeout(800)
        frames = page.frames
        framed = (
            len(frames) > 1
            and frames[1].url.startswith(base)
            and "Код" in (await frames[1].inner_text("body") if not frames[1].is_detached() else "")
        )
        report.ok(not framed, "ordinary /login is refused inside the Telegram Web iframe")
        await context.close()

        # --- 11b. «Добавить на главный экран» with a simulated SDK ---------------------------------
        async def shortcut(label: str, *, version: float = 8.0, wait: int = 400, **home):
            context = await new_context()
            harness.state["init_data"] = init_data()
            harness.state["version"] = version
            harness.state["home"] = {
                "methods": True,
                "status": "missed",
                "after": None,
                "event": False,
                **home,
            }
            await harness.install(context, f"{base}/tma")
            page = await open_app(context, harness, report)
            await wait_path(page, "/")
            await child(page).goto(f"{base}/settings")
            await page.wait_for_timeout(wait)
            frame = child(page)
            section = frame.locator("[data-home-screen]")
            visible = await section.is_visible()
            shown = [
                await frame.locator(f"[data-home-screen] [data-state={n}]").is_visible()
                for n in ("offer", "unknown", "added", "requested")
            ]
            button = await frame.locator("[data-action=add-home-screen]").is_visible()
            calls = [n for n, _ in await frame.evaluate("window.__tg.calls")]
            return context, page, frame, visible, shown, button, calls

        # version below 8.0: nothing is offered and the API is not touched
        context, page, frame, visible, shown, button, calls = await shortcut("old", version=7.9)
        report.ok(
            not visible and not button and "checkHomeScreenStatus" not in calls,
            "shortcut: API older than 8.0 -> section hidden, no calls",
        )
        await context.close()

        # methods missing in a new-enough client
        context, page, frame, visible, shown, button, calls = await shortcut(
            "no-methods", methods=False
        )
        report.ok(not visible and not button, "shortcut: methods missing -> section hidden")
        await context.close()

        # unsupported
        context, page, frame, visible, shown, button, calls = await shortcut(
            "unsupported", status="unsupported"
        )
        report.ok(
            not visible and not button and "addToHomeScreen" not in calls,
            "shortcut: status unsupported -> section hidden",
        )
        await context.close()

        # added
        context, page, frame, visible, shown, button, calls = await shortcut(
            "added", status="added"
        )
        report.ok(
            visible and shown == [False, False, True, False] and not button,
            "shortcut: status added -> «Ярлык уже добавлен.» and no button",
            str(shown),
        )
        report.ok(
            "Ярлык уже добавлен." in await frame.inner_text("[data-home-screen]")
            and "addToHomeScreen" not in calls,
            "shortcut: added -> the text is shown and nothing is requested",
        )
        await context.close()

        # missed: offered, the call happens only on the click
        context, page, frame, visible, shown, button, calls = await shortcut(
            "missed", status="missed"
        )
        report.ok(
            visible and shown == [True, False, False, False] and button,
            "shortcut: status missed -> the offer and the button",
            str(shown),
        )
        report.ok(
            "addToHomeScreen" not in calls, "shortcut: addToHomeScreen is not called before a click"
        )
        await frame.locator("[data-home-screen]").scroll_into_view_if_needed()
        await page.screenshot(path=OUT / "phone-shortcut.png")
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(2000)
        frame = child(page)
        calls = [n for n, _ in await frame.evaluate("window.__tg.calls")]
        report.ok(
            calls.count("addToHomeScreen") == 1, "shortcut: one click -> one addToHomeScreen call"
        )
        text = await frame.inner_text("[data-home-screen]")
        report.ok(
            "Запрос отправлен" in text and "Ярлык уже добавлен" not in text,
            "shortcut: after the call without a confirmation it is not declared a success",
            text,
        )
        await context.close()

        # unknown: can be requested, but is not claimed to be missing
        context, page, frame, visible, shown, button, calls = await shortcut(
            "unknown", status="unknown"
        )
        text = await frame.inner_text("[data-home-screen]")
        report.ok(
            visible and shown == [False, True, False, False] and button,
            "shortcut: status unknown -> can request the shortcut",
            str(shown),
        )
        report.ok(
            "ещё нет" not in text
            and "не добавлен" not in text
            and "Ярлык уже добавлен" not in text,
            "shortcut: unknown -> no claim that the shortcut is missing or present",
            text,
        )
        report.ok(
            "addToHomeScreen" not in calls, "shortcut: unknown -> nothing requested without a click"
        )
        await context.close()

        # the device never answers: the same as unknown after a moment
        context, page, frame, visible, shown, button, calls = await shortcut(
            "silent", status=None, wait=2200
        )
        report.ok(
            visible
            and shown == [False, True, False, False]
            and button
            and "addToHomeScreen" not in calls,
            "shortcut: no status from the device -> unknown, button usable, no call",
            str(shown),
        )
        await context.close()

        # the homeScreenAdded event after the click -> added
        context, page, frame, visible, shown, button, calls = await shortcut(
            "event", status="unknown", event=True
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(300)
        frame = child(page)
        report.ok(
            "Ярлык уже добавлен." in await frame.inner_text("[data-home-screen]")
            and not await frame.locator("[data-action=add-home-screen]").is_visible(),
            "shortcut: homeScreenAdded event -> added",
        )
        await context.close()

        # a later status check that says added (device without the event)
        context, page, frame, visible, shown, button, calls = await shortcut(
            "recheck", status="unknown", after="added"
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(2000)
        report.ok(
            "Ярлык уже добавлен." in await child(page).inner_text("[data-home-screen]"),
            "shortcut: status says added after the request -> added",
        )
        await context.close()

        def state_text(frame):
            return frame.inner_text("[data-home-screen]")

        # regression: homeScreenAdded, then a later check says unknown -> stays added
        context, page, frame, *_ = await shortcut(
            "added-then-unknown", status="unknown", event=True
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(2600)  # the delayed check has answered «unknown» by now
        frame = child(page)
        report.ok(
            "Ярлык уже добавлен." in await state_text(frame)
            and not await frame.locator("[data-action=add-home-screen]").is_visible()
            and "Запрос отправлен" not in await state_text(frame),
            "shortcut: homeScreenAdded, then unknown -> added is kept, no button",
        )
        await context.close()

        # regression: the first check is silent; homeScreenAdded arrives before the fallback timer
        context, page, frame, *_ = await shortcut("silent-then-event", status=None, wait=300)
        await frame.evaluate("window.__tg.handlers.homeScreenAdded()")
        await page.wait_for_timeout(2400)  # past the 1.5 s fallback
        frame = child(page)
        report.ok(
            "Ярлык уже добавлен." in await state_text(frame)
            and not await frame.locator("[data-action=add-home-screen]").is_visible(),
            "shortcut: silent first check, homeScreenAdded before the fallback timer -> stays added",
        )
        await context.close()

        # regression: a stale answer of an older check (slow «missed») after the newer «added»
        context, page, frame, *_ = await shortcut(
            "stale-after-added", sequence=["missed", "added"], delays=[2800, 0], wait=300
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(3600)  # newer «added» at ~1.8 s, stale «missed» at 2.8 s
        frame = child(page)
        report.ok(
            "Ярлык уже добавлен." in await state_text(frame)
            and not await frame.locator("[data-action=add-home-screen]").is_visible(),
            "shortcut: a stale older answer does not undo a newer added",
        )
        await context.close()

        # an older «missed» after a newer «unknown» does not bring the offer back
        context, page, frame, *_ = await shortcut(
            "stale-after-unknown", sequence=["missed", "unknown"], delays=[2800, 0], wait=300
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(3600)
        frame = child(page)
        text = await state_text(frame)
        report.ok(
            "Запрос отправлен" in text and "Добавь ХВОСТ" not in text,
            "shortcut: a stale older missed after a newer unknown is ignored",
            text,
        )
        await context.close()

        # addToHomeScreen throws: a neutral error, a retry only by the owner's click
        context, page, frame, *_ = await shortcut("throws", status="missed", throws=True)
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(2600)  # long enough for any automatic retry to show up
        frame = child(page)
        text = await state_text(frame)
        calls = [n for n, _ in await frame.evaluate("window.__tg.calls")]
        report.ok(
            "Не удалось запросить ярлык. Попробуй ещё раз." in text
            and "Запрос отправлен" not in text
            and "Ярлык уже добавлен" not in text
            and await frame.locator("[data-action=add-home-screen]").is_visible(),
            "shortcut: addToHomeScreen throws -> neutral error and the button stays",
            text,
        )
        report.ok(
            calls.count("addToHomeScreen") == 1, "shortcut: no automatic retry after an error"
        )
        await frame.click("[data-action=add-home-screen]")
        await page.wait_for_timeout(300)
        calls = [n for n, _ in await child(page).evaluate("window.__tg.calls")]
        report.ok(
            calls.count("addToHomeScreen") == 2, "shortcut: a retry only on the owner's click"
        )
        await context.close()
        harness.state["version"] = 8.0
        harness.state["home"] = {"methods": True, "status": "missed", "after": None, "event": False}

        # --- 12. wide screenshots, dark theme --------------------------------------------------------
        for scheme, w, h, name in (
            ("light", 1100, 900, "wide-home"),
            ("dark", 390, 844, "phone-home-dark"),
        ):
            context = await new_context(w, h)
            harness.state["init_data"] = init_data()
            harness.state["scheme"] = scheme
            await harness.install(context, f"{base}/tma")
            page = await open_app(context, harness, report)
            await wait_path(page, "/")
            await settle(page)
            report.ok(
                await child(page).evaluate("document.documentElement.getAttribute('data-scheme')")
                == scheme,
                f"theme scheme {scheme} applied",
            )
            await page.screenshot(path=OUT / f"{name}.png")
            report.ok(
                await child(page).evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ),
                f"no horizontal scroll ({name})",
            )
            await context.close()

        await browser.close()

    report.ok(
        not harness.console, "no console errors / CSP violations", "; ".join(harness.console[:4])
    )
    server.should_exit = True
    await serving
    await core.close()
    print(f"\nscreenshots: {OUT}")
    if report.failures:
        print(f"{len(report.failures)} check(s) failed", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


async def _today(uow) -> date:
    return uow.today_now


async def _first_note_task(uow) -> str:
    from sqlalchemy import select

    from xboct.storage.models import Task

    return await uow.session.scalar(select(Task.id).where(Task.note.is_not(None)).limit(1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", help="path to a Chromium binary")
    parser.add_argument(
        "--third-party-cookie-phaseout",
        action="store_true",
        help="simulate blocked third-party cookies (the partitioned cookie must still work)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.chromium, args.third_party_cookie_phaseout)))
