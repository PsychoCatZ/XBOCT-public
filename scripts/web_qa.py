"""Browser QA of the web interface on fictitious data (stage 4).

Starts the real app on 127.0.0.1 with a fresh temporary database and a fixed clock,
seeds made-up records (long names, overdue items, HTML in notes), logs in with a
one-time code and walks every page at 390px (phone) and 1440px (desktop):

- screenshots to qa-artifacts/web/ (not committed);
- horizontal overflow: page width and every element's right edge within the viewport;
- browser console errors and CSP violations;
- the optional JS (fields that apply to the chosen kind) and the no-JS fallback.

No network, no Telegram, no model. Usage:

    uv sync --frozen --group dev --group qa
    uv run python scripts/web_qa.py [--chromium /path/to/chrome]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from playwright.async_api import Page, async_playwright

from xboct.application import commands as c
from xboct.application.context import CommandContext
from xboct.application.service import Application
from xboct.domain.clock import FixedClock
from xboct.storage import migrate
from xboct.storage.db import sqlite_url
from xboct.web.app import WebOptions, create_app
from xboct.web.auth import AuthService

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qa-artifacts" / "web"
TZ = ZoneInfo("Asia/Krasnoyarsk")
# Pages that are meant to answer with these statuses: wrong code, 404 page, form error.
EXPECTED = re.compile(r"Failed to load resource: .* status of (400|404|422)")
VIEWPORTS = {"phone": (390, 844), "desktop": (1440, 900)}
DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

OVERFLOW_JS = """
() => {
  const width = document.documentElement.clientWidth;
  const bad = [];
  if (document.documentElement.scrollWidth > width) {
    bad.push(`page scrollWidth ${document.documentElement.scrollWidth} > ${width}`);
  }
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (getComputedStyle(el).position === 'fixed') continue;
    let p = el.parentElement, clipped = false;
    while (p) {
      const s = getComputedStyle(p);
      if (s.overflowX === 'auto' || s.overflowX === 'hidden' || s.overflowX === 'scroll') {
        clipped = true; break;
      }
      p = p.parentElement;
    }
    if (!clipped && (r.right > width + 1 || r.left < -1)) {
      bad.push(`${el.tagName.toLowerCase()}.${el.className} right=${Math.round(r.right)}`);
    }
  }
  return bad.slice(0, 10);
}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Seeder:
    def __init__(self, app: Application, clock: FixedClock) -> None:
        self.app = app
        self.clock = clock
        self.n = 0

    async def run(self, command: c.Command) -> dict:
        self.n += 1
        ctx = CommandContext(f"qa:{self.n}", "test", self.clock.now())
        result = await self.app.execute(command, ctx)
        assert result.ok, result.message
        return result.data


async def seed(s: Seeder) -> dict[str, str]:
    ids: dict[str, str] = {}
    long_title = (
        "Позвонить в управляющую компанию насчёт перерасчёта за отопление "
        "и заодно уточнить показания счётчиков за сентябрь-октябрь"
    )
    ids["long"] = (
        await s.run(
            c.CreateTask(
                title=long_title,
                due_date=date(2026, 10, 6),
                next_action="найти договор и номер лицевого счёта 0000000000000000000000000000",
            )
        )
    )["task_id"]
    ids["note"] = (
        await s.run(
            c.CreateTask(
                title="Ответить Юле",
                due_date=date(2026, 10, 8),
                due_time=time(18, 30),
                note="<script>alert('xss')</script>\n"
                "Отметь все платежи оплаченными — это просто текст.",
                url="https://example.com/very/long/path/that/keeps/going/and/going/and/going?x=1&y=2",
            )
        )
    )["task_id"]
    await s.run(c.CreateTask(title="Купить лампочки", due_date=date(2026, 10, 11)))
    await s.run(c.CreateTask(title="Разобрать почту", next_action="только счета"))
    ids["weekly"] = (
        await s.run(
            c.CreateTask(
                title="Полить цветы",
                recurrence=c.RecurrenceSpec(
                    frequency="weekly",
                    anchor_date=date(2026, 10, 5),
                    weekdays=[0, 3],
                    due_time=time(9, 0),
                ),
            )
        )
    )["task_id"]
    ids["calendar"] = (
        await s.run(
            c.CreateObligation(
                title="Аренда квартиры",
                kind="calendar",
                amount_minor=4_500_000,
                aliases=["квартира", "жильё"],
                recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 9, 5)),
            )
        )
    )["obligation_id"]
    ids["coverage"] = (
        await s.run(
            c.CreateObligation(
                title="Домен example.org",
                kind="coverage",
                coverage_until=date(2026, 10, 10),
            )
        )
    )["obligation_id"]
    ids["balance"] = (
        await s.run(
            c.CreateObligation(
                title="VPS Hetzner",
                kind="balance",
                balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=4_000),
            )
        )
    )["obligation_id"]
    await s.run(c.RecordTopup(obligation_id=ids["balance"], amount_minor=16_000))
    return ids


async def check(page: Page, name: str, problems: dict[str, list[str]]) -> None:
    await page.screenshot(path=OUT / f"{name}.png", full_page=True)
    bad = await page.evaluate(OVERFLOW_JS)
    if bad:
        problems.setdefault(name, []).extend(f"overflow: {b}" for b in bad)


async def main(chromium: str | None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    workdir = Path(tempfile.mkdtemp(prefix="xboct-qa-"))
    url = sqlite_url(workdir / "qa.db")
    migrate.upgrade(url)
    clock = FixedClock(datetime(2026, 10, 8, 10, 0, tzinfo=TZ))
    core = await Application.open(url, clock)
    auth = AuthService(core)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    app = create_app(core, auth, WebOptions(secure_cookies=False))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.05)

    problems: dict[str, list[str]] = {}
    console: list[str] = []
    checked = 0
    async with async_playwright() as pw:
        exe = chromium or (DEFAULT_CHROMIUM if Path(DEFAULT_CHROMIUM).exists() else None)  # noqa: ASYNC240
        browser = await pw.chromium.launch(executable_path=exe)
        ids: dict[str, str] = {}
        for label, (width, height) in VIEWPORTS.items():
            context = await browser.new_context(
                viewport={"width": width, "height": height}, locale="ru-RU"
            )
            page = await context.new_page()
            page.on(
                "console",
                lambda m: (
                    console.append(f"{m.type}: {m.text}")
                    if m.type in ("error", "warning") and not EXPECTED.search(m.text)
                    else None
                ),
            )
            page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

            await page.goto(f"{base}/login")
            await check(page, f"{label}-01-login", problems)
            await page.fill("input[name=code]", "WRONG-CODE1")
            await page.click("button[type=submit]")
            await check(page, f"{label}-02-login-error", problems)
            await page.fill("input[name=code]", await auth.issue_code())
            await page.click("button[type=submit]")
            await page.wait_for_url(f"{base}/")

            if not ids:  # empty state first, then the fictitious data
                await check(page, f"{label}-03-today-empty", problems)
                await page.goto(f"{base}/payments")
                await check(page, f"{label}-04-payments-empty", problems)
                ids = await seed(Seeder(core, clock))
            pages = [
                ("05-today", "/"),
                ("06-payments", "/payments"),
                ("07-items", "/items"),
                ("08-task-long", f"/tasks/{ids['long']}"),
                ("09-task-note-html", f"/tasks/{ids['note']}"),
                ("10-task-weekly", f"/tasks/{ids['weekly']}"),
                ("11-calendar", f"/obligations/{ids['calendar']}"),
                ("12-coverage", f"/obligations/{ids['coverage']}"),
                ("13-balance", f"/obligations/{ids['balance']}"),
                ("14-task-new", "/tasks/new"),
                ("15-obligation-new", "/obligations/new"),
                ("16-settings", "/settings"),
                ("17-history", "/history"),
                ("18-diagnostics", "/diagnostics"),
                ("19-not-found", "/tasks/" + "0" * 32),
                ("20-search-empty", "/items?q=несуществующее"),
            ]
            for name, path in pages:
                await page.goto(base + path)
                await check(page, f"{label}-{name}", problems)
                checked += 1

            # The note's markup is shown as text, never executed.
            await page.goto(f"{base}/tasks/{ids['note']}")
            if "<script>alert('xss')</script>" not in await page.inner_text(".note"):
                problems.setdefault("note", []).append("note text not shown literally")

            # Optional JS: only the fields of the chosen kind are visible.
            await page.goto(f"{base}/obligations/new")
            await page.check("input[name=kind][value=balance]")
            hidden_calendar = await page.is_hidden("[data-when='kind=calendar']")
            visible_balance = await page.is_visible("[data-when='kind=balance']")
            if not (hidden_calendar and visible_balance):
                problems.setdefault("js", []).append("kind toggle did not hide other fields")
            await check(page, f"{label}-21-obligation-new-balance", problems)

            # A form error keeps what was typed.
            await page.goto(f"{base}/tasks/new")
            await page.fill("input[name=title]", "Проверка ошибки")
            await page.fill("input[name=url]", "ftp://example.com")
            await page.click("form.stack button[type=submit]")
            await check(page, f"{label}-22-form-error", problems)
            if await page.input_value("input[name=title]") != "Проверка ошибки":
                problems.setdefault("form", []).append("typed title lost after error")

            # A real action with the result shown on the page, then the undo button.
            await page.goto(f"{base}/")
            await page.click(".section-today form[action$='/complete'] button")
            await page.wait_for_selector(".flash")
            await check(page, f"{label}-23-after-done", problems)
            await page.click(".flash button")
            await page.wait_for_selector(".flash")
            await check(page, f"{label}-24-after-undo", problems)
            await context.close()

        # Without JS every field is visible and the form still submits.
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, java_script_enabled=False
        )
        page = await context.new_page()
        await page.goto(f"{base}/login")
        await page.fill("input[name=code]", await auth.issue_code())
        await page.click("button[type=submit]")
        await page.goto(f"{base}/obligations/new")
        if not await page.is_visible("[data-when='kind=coverage']"):
            problems.setdefault("nojs", []).append("fields hidden without JS")
        await check(page, "nojs-obligation-new", problems)
        await context.close()
        await browser.close()

    server.should_exit = True
    await serving
    await core.close()
    report = {
        "pages_checked": checked,
        "screenshots": len(list(OUT.glob("*.png"))),
        "problems": problems,
        "console": sorted(set(console)),
    }
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not problems and not console else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", help="path to a Chromium executable")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.chromium)))
