"""Web access (A27): one-time Telegram codes, sessions, CSRF/origin, throttling, no secrets.

All of it runs without a language model and without a real Telegram bot.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import pytest

from tests.integration.web import Web, hidden
from xboct.storage.models import AuthCode, LoginAttempt, OutboxMessage, WebSession
from xboct.web.app import safe_path


@pytest.fixture
async def web(x):
    client = Web(x)
    yield client
    await client.close()


async def test_pages_need_login_and_login_sets_safe_cookie(web, x):
    response = await web.get("/")
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert (await web.get("/tasks/new")).status_code == 303

    response = await web.login()
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookie = response.headers["set-cookie"]
    assert "xboct_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert (await web.get("/")).status_code == 200
    [code] = await x.all(AuthCode)
    assert code.used_at is not None
    [session] = await x.all(WebSession)
    assert len(session.token_hash) == 64  # only the hash is stored
    assert session.token_hash not in cookie


async def test_secure_flag_when_configured(x):
    web = Web(x, secure_cookies=True)
    try:
        response = await web.login()
        assert "Secure" in response.headers["set-cookie"]
    finally:
        await web.close()


async def test_code_is_one_time_and_expires(web, x):
    code = await web.auth.issue_code()
    assert (await web.submit_code(code)).status_code == 303
    web.client.cookies.clear()
    again = await web.submit_code(code)
    assert again.status_code == 400
    assert "Код не подошёл" in again.text

    stale = await web.auth.issue_code()
    x.clock.advance(minutes=6)
    assert (await web.submit_code(stale)).status_code == 400

    first = await web.auth.issue_code()
    second = await web.auth.issue_code()  # a new code invalidates the older one
    assert (await web.submit_code(first)).status_code == 400
    assert (await web.submit_code(second)).status_code == 303


async def test_code_is_case_and_dash_insensitive(web):
    code = await web.auth.issue_code()
    assert (await web.submit_code(code.replace("-", "").lower())).status_code == 303


async def test_five_wrong_tries_kill_the_code(web):
    code = await web.auth.issue_code()
    for _ in range(5):
        assert (await web.submit_code("AAAAA-AAAAA")).status_code == 400
    assert (await web.submit_code(code)).status_code == 400  # brute force cannot win later


async def test_throttling_per_ip_and_window(web, x):
    for _ in range(10):
        await web.submit_code("WRONG-WRONG")
    code = await web.auth.issue_code()
    response = await web.submit_code(code)
    assert response.status_code == 429
    assert "Слишком много" in response.text
    x.clock.advance(minutes=16)
    assert (await web.submit_code(await web.auth.issue_code())).status_code == 303
    attempts = await x.all(LoginAttempt)
    assert all(len(a.ip_hash) == 64 and "203.0.113.5" not in a.ip_hash for a in attempts)


async def test_session_expires_and_can_be_revoked(web, x):
    await web.login()
    x.clock.advance(days=7, minutes=1)
    response = await web.get("/")
    assert (response.status_code, response.headers["location"]) == (303, "/login")

    await web.login()
    html = await web.page("/")
    response = await web.post("/logout", hidden(html, "/logout"))
    assert response.headers["location"] == "/login"
    assert (await web.get("/")).status_code == 303

    await web.login()
    other = Web(x)
    try:
        await other.login()
        html = await web.page("/settings")
        await web.post("/sessions/revoke-all", hidden(html, "/sessions/revoke-all"))
        assert (await other.get("/")).status_code == 303  # every device is logged out
    finally:
        await other.close()


async def test_csrf_and_origin_are_required_for_every_change(web, x):
    await web.login()
    html = await web.page("/tasks/new")
    good = {**hidden(html, "/tasks"), "title": "Проверка CSRF"}

    no_token = {k: v for k, v in good.items() if k != "csrf"}
    assert (await web.post("/tasks", no_token)).status_code == 403
    assert (await web.post("/tasks", {**good, "csrf": "0" * 40})).status_code == 403
    evil = await web.post("/tasks", good, headers={"origin": "https://evil.example"})
    assert evil.status_code == 403
    web.client.headers.pop("origin")
    assert (await web.post("/tasks", good)).status_code == 403  # neither Origin nor Referer
    referer = await web.post("/tasks", good, headers={"referer": "http://testserver/tasks/new"})
    assert referer.status_code == 303
    from xboct.storage.models import Task

    assert [t.title for t in await x.all(Task)] == ["Проверка CSRF"]
    # No mutation through GET.
    task_id = referer.headers["location"].split("/")[2].split("?")[0]
    assert (await web.get(f"/tasks/{task_id}/complete")).status_code == 405


async def test_login_form_itself_is_csrf_protected(web):
    code = await web.auth.issue_code()
    page = await web.get("/login")
    token = hidden(page.text, "/login")["login_csrf"]
    web.client.cookies.clear()  # the double-submit cookie is missing
    response = await web.post("/login", {"code": code, "login_csrf": token})
    assert response.status_code == 403
    page = await web.get("/login")
    token = hidden(page.text, "/login")["login_csrf"]
    cross = await web.post(
        "/login", {"code": code, "login_csrf": token}, headers={"origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    assert (await web.submit_code(code)).status_code == 303  # the code was not burnt


async def test_logs_never_contain_codes_or_tokens(web, caplog):
    caplog.set_level(logging.DEBUG)
    code = await web.auth.issue_code()
    await web.submit_code("WRONG-WRONG")
    response = await web.submit_code(code)
    token = response.cookies.get("xboct_session") or web.client.cookies.get("xboct_session")
    assert token
    html = await web.page("/tasks/new")
    await web.post("/tasks", {**hidden(html, "/tasks"), "title": "Лог"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (code, code.replace("-", ""), token, "203.0.113.5"):
        assert secret not in text
    assert "web login ok" in text


async def test_security_headers(web):
    response = await web.get("/login")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "<script>" not in response.text  # no inline scripts: CSP stays strict


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/tasks/1", "/tasks/1"),
        ("//evil.example/x", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/ok\r\nSet-Cookie: x", "/"),
        (None, "/"),
    ],
)
def test_safe_path(value, expected):
    assert safe_path(value, "/") == expected


async def test_telegram_login_code_is_sent_then_redacted(bot):
    bot.say("/login")
    await bot.tick()
    reply = bot.sent[-1]
    assert "Код для входа" in reply
    code = re.search(r"[A-Z2-9]{5}-[A-Z2-9]{5}", reply).group(0)
    # After delivery the stored copy of the message no longer has the code.
    [message] = await bot.outbox(OutboxMessage.sensitive.is_(True))
    assert message.status == "sent"
    assert code not in message.text
    web = Web(bot.x)
    try:
        assert (await web.submit_code(code)).status_code == 303
    finally:
        await web.close()


async def test_stranger_gets_no_login_code(bot):
    from tests.integration.rt import STRANGER

    bot.say("/login", sender=STRANGER)
    await bot.tick()
    assert not any("Код" in text for text in bot.sent)
    assert await bot.rows(AuthCode) == []


def test_session_ttl_default_is_a_week():
    from xboct.web.auth import DEFAULT_SESSION_TTL

    assert timedelta(days=7) == DEFAULT_SESSION_TTL
