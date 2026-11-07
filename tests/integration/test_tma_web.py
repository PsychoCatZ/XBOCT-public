"""Telegram Mini App: sign-in, session, framing, CSRF and the redirects around them.

The signer is independent of the module under test (docs of Telegram: sorted key=value
lines, secret = HMAC_SHA256("WebAppData", token)). Browser behaviour (cookies kept inside
Telegram Web's iframe, theme, bottom bar) is checked by scripts/tma_qa.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from tests.integration.web import Web, hidden, text_of
from xboct.storage.models import WebSession
from xboct.web import tma as tma_module
from xboct.web.app import TELEGRAM_FRAME_ANCESTORS
from xboct.web.auth import (
    MAX_FAILURES_PER_IP,
    MAX_SESSIONS_PER_WINDOW,
    AuthService,
    LoginThrottled,
    csrf_token,
)
from xboct.web.tma import MAX_BODY_BYTES, TmaConfig

TOKEN = "123456:SYNTHETIC-TEST-TOKEN"
OWNER = 424242
TMA_COOKIE = "__Host-xboct_tma"
HINT_COOKIE = "__Host-xboct_tma_ctx"
SDK = "https://telegram.org/js/telegram-web-app.js"


@pytest.fixture
async def tma(x):
    web = Web(x, secure_cookies=True, tma=TmaConfig(TOKEN, OWNER))
    yield web
    await web.close()


def signed(
    x, *, age: timedelta = timedelta(0), user_id: int = OWNER, token: str = TOKEN, **extra: str
) -> str:
    fields = {
        "auth_date": str(int((x.clock.now() - age).timestamp())),
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
        "query_id": "AAHsynthetic",
        "signature": "c2lnbmF0dXJl",
        **extra,
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def sign_in(web: Web, x, **kw) -> httpx.Response:
    return await web.client.post("/tma/auth", json={"init_data": signed(x, **kw)})


def cookies_set(response: httpx.Response) -> dict[str, str]:
    """Set-Cookie header text by cookie name."""
    out = {}
    for header in response.headers.get_list("set-cookie"):
        out[header.split("=", 1)[0]] = header
    return out


async def sessions(x) -> list[WebSession]:
    return await x.all(WebSession)


# --- sign-in -----------------------------------------------------------------------------


async def test_the_owner_signs_in_and_gets_a_partitioned_session_cookie(tma, x):
    response = await sign_in(tma, x)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    header = cookies_set(response)[TMA_COOKIE]
    for attribute in ("Secure", "HttpOnly", "SameSite=None", "Partitioned", "Path=/"):
        assert attribute in header
    assert "Domain" not in header
    assert f"Max-Age={12 * 3600}" in header
    token = header.split("=", 1)[1].split(";", 1)[0]
    assert token not in response.text
    [row] = await sessions(x)
    assert row.token_hash != token
    assert len(row.token_hash) == 64
    assert row.expires_at - row.created_at == timedelta(hours=12)  # absolute expiry is stored
    assert (await tma.get("/tma/session")).status_code == 200


async def test_cookie_is_not_the_ordinary_one(tma, x):
    response = await sign_in(tma, x)
    assert "xboct_session" not in cookies_set(response)


@pytest.mark.parametrize(
    ("kwargs", "status", "error"),
    [
        ({"token": "999999:OTHER"}, 401, "invalid"),
        ({"user_id": OWNER + 1}, 403, "invalid"),
        ({"age": timedelta(minutes=16)}, 401, "stale"),
        ({"age": -timedelta(minutes=2)}, 401, "future"),
    ],
)
async def test_refused_sign_ins_create_nothing(tma, x, kwargs, status, error):
    response = await sign_in(tma, x, **kwargs)
    assert (response.status_code, response.json()) == (status, {"ok": False, "error": error})
    assert await sessions(x) == []
    # nothing is granted: at most a cookie is cleared
    assert all("Max-Age=0" in header for header in cookies_set(response).values())


async def test_the_page_decides_nothing_the_server_does(tma, x):
    # initDataUnsafe, a mode parameter or a Telegram header grant nothing.
    body = {"init_data": "", "initDataUnsafe": {"user": {"id": OWNER}}}
    assert (await tma.client.post("/tma/auth", json=body)).status_code == 401
    page = await tma.get("/payments?mode=tma&tgWebAppData=x", headers={"x-telegram-user": "1"})
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    assert await sessions(x) == []


@pytest.mark.parametrize("content", [b"{bad", b"[1]", b'{"init_data": 5}', b'{"other": "x"}'])
async def test_malformed_bodies_are_refused(tma, x, content):
    response = await tma.client.post(
        "/tma/auth", content=content, headers={"content-type": "application/json"}
    )
    assert response.status_code == 401
    assert await sessions(x) == []


async def test_content_type_and_origin_are_required(tma, x):
    plain = await tma.client.post(
        "/tma/auth", content=signed(x), headers={"content-type": "text/plain"}
    )
    assert plain.status_code == 415
    for origin in ("https://evil.example", "https://web.telegram.org"):
        foreign = await tma.client.post(
            "/tma/auth", json={"init_data": signed(x)}, headers={"origin": origin}
        )
        assert foreign.status_code == 403
    assert await sessions(x) == []


async def test_body_size_is_limited_before_parsing(tma, x):
    big = json.dumps({"init_data": "x" * MAX_BODY_BYTES})
    declared = await tma.client.post(
        "/tma/auth", content=big, headers={"content-type": "application/json"}
    )
    assert declared.status_code == 413

    async def chunks():  # no Content-Length: the stream itself is counted
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    streamed = await tma.client.post(
        "/tma/auth", content=chunks(), headers={"content-type": "application/json"}
    )
    assert streamed.status_code == 413
    assert await sessions(x) == []


async def test_failures_are_throttled_like_the_code_login(tma, x):
    for _ in range(MAX_FAILURES_PER_IP):
        assert (await sign_in(tma, x, token="999999:OTHER")).status_code == 401
    blocked = await sign_in(tma, x)  # even a valid one is refused now
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "throttled"
    assert await sessions(x) == []


async def test_stale_windows_are_not_counted_as_attacks(tma, x):
    for _ in range(MAX_FAILURES_PER_IP + 2):
        assert (await sign_in(tma, x, age=timedelta(hours=1))).status_code == 401
    assert (await sign_in(tma, x)).status_code == 200


async def test_session_creation_is_rate_limited(tma, x):
    for _ in range(MAX_SESSIONS_PER_WINDOW):
        assert (await sign_in(tma, x)).status_code == 200
    assert (await sign_in(tma, x)).status_code == 429
    assert len(await sessions(x)) == MAX_SESSIONS_PER_WINDOW


async def test_nothing_secret_reaches_logs_or_urls(tma, x, caplog):
    caplog.set_level(logging.DEBUG)
    raw = signed(x)
    response = await tma.client.post("/tma/auth", json={"init_data": raw})
    await sign_in(tma, x, token="999999:OTHER")
    await tma.get("/")
    cookie = cookies_set(response)[TMA_COOKIE].split("=", 1)[1].split(";", 1)[0]
    hash_value = dict(p.split("=", 1) for p in raw.split("&"))["hash"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (raw, hash_value, TOKEN, cookie, "AAHsynthetic", "203.0.113.5"):
        assert secret not in text
    assert "tma sign-in ok" in text


async def test_without_a_configured_bot_the_mini_app_is_off(x):
    web = Web(x, secure_cookies=True)
    try:
        assert (await web.get("/tma")).status_code == 404
        assert (await web.client.post("/tma/auth", json={"init_data": "x"})).status_code == 404
    finally:
        await web.close()


# --- pages, framing, headers -------------------------------------------------------------


async def test_the_entry_page_and_its_headers(tma):
    response = await tma.get("/tma")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert f"script-src 'self' {SDK}" in csp
    ancestors = csp.split("frame-ancestors ", 1)[1].split(";", 1)[0].split()
    assert ancestors == list(TELEGRAM_FRAME_ANCESTORS)
    assert "*" not in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "x-frame-options" not in response.headers
    assert "<script>" not in response.text  # no inline script, even for the adapter
    assert 'src="/static/tma-login.js"' in response.text


async def test_app_pages_in_mini_app_mode_load_the_sdk_and_the_adapter(tma, x):
    await sign_in(tma, x)
    page = await tma.get("/")
    assert page.status_code == 200
    assert '<body class="tma">' in page.text
    assert f'<script src="{SDK}" defer>' in page.text
    assert 'src="/static/tma.js"' in page.text
    assert 'class="bottom-nav"' in page.text
    assert 'class="top"' not in page.text  # the top bar is replaced by the bottom one
    assert "x-frame-options" not in page.headers
    assert f"script-src 'self' {SDK}" in page.headers["content-security-policy"]
    assert "viewport-fit=cover" in page.text


async def test_the_ordinary_web_keeps_its_protection(tma, x):
    await tma.login()  # ordinary sign-in by code
    page = await tma.get("/")
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "telegram" not in csp
    assert page.headers["x-frame-options"] == "DENY"
    assert 'class="tma"' not in page.text
    assert "telegram-web-app.js" not in page.text
    assert "tma.js" not in page.text
    login = await tma.get("/login")
    assert login.headers["x-frame-options"] == "DENY"


async def test_a_mini_app_browser_context_may_be_framed_but_runs_no_foreign_script(tma):
    tma.client.cookies.set(HINT_COOKIE, "1", domain="testserver.local")
    response = await tma.get("/login")
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors https://web.telegram.org" in csp
    assert "telegram.org/js" not in csp
    assert "x-frame-options" not in response.headers


async def test_regular_login_clears_the_mini_app_hint(tma):
    response = await tma.login()
    header = cookies_set(response)[HINT_COOKIE]
    assert "Max-Age=0" in header
    assert "Partitioned" in header


async def test_nav_labels_in_the_mini_app(tma, x):
    await sign_in(tma, x)
    text = text_of(await tma.page("/"))
    for label in ("Главная", "Платежи", "Все", "История", "Ещё"):
        assert label in text


# --- sessions: expiry, redirects without loops -------------------------------------------


def location(response: httpx.Response) -> str:
    return response.headers["location"]


async def test_expired_session_returns_to_the_mini_app_entry_not_to_login(tma, x):
    await sign_in(tma, x)
    x.clock.advance(hours=12, seconds=1)  # absolute expiry
    response = await tma.get("/payments")
    assert response.status_code == 303
    assert location(response) == "/tma?next=%2Fpayments"
    entry = await tma.get(location(response))
    assert entry.status_code == 200  # the entry page itself never redirects anywhere
    assert 'data-next="/payments"' in entry.text


async def test_revoked_session_follows_the_same_path_and_the_chain_ends(tma, x):
    await sign_in(tma, x)
    await tma.auth.revoke_all()
    chain = ["/"]
    for _ in range(5):
        response = await tma.get(chain[-1])
        if response.status_code != 303:
            break
        chain.append(location(response))
    assert chain == ["/", "/tma?next=%2F"]
    assert (await tma.get("/tma/session")).status_code == 401


async def test_without_the_hint_the_old_login_redirect_is_unchanged(tma):
    response = await tma.get("/")
    assert (response.status_code, location(response)) == (303, "/login")
    assert (await tma.get("/login")).status_code == 200


async def test_login_page_is_reachable_from_the_mini_app_context(tma, x):
    await sign_in(tma, x)
    await tma.auth.revoke_all()
    assert (await tma.get("/login")).status_code == 200  # the way out of a blocked-cookie dead end


async def test_opening_the_entry_page_never_trusts_the_old_cookie(tma, x):
    # Every open through /tma must pass the signed-identity check, cookie or not.
    await sign_in(tma, x)
    for target in ("/tma", "/tma?next=/payments", "/tma?next=//evil.example"):
        page = await tma.get(target)
        assert page.status_code == 200
        assert 'data-state="working"' in page.text
    assert 'data-next="/payments"' in (await tma.get("/tma?next=/payments")).text
    assert 'data-next="/"' in (await tma.get("/tma?next=//evil.example")).text
    assert 'data-next="/"' in (await tma.get("/tma?next=https://evil.example")).text
    assert (await tma.get("/payments")).status_code == 200  # inner navigation needs no new sign-in


async def test_reopening_with_the_owner_replaces_the_previous_session(tma, x):
    await sign_in(tma, x)
    first = tma.client.cookies.get(TMA_COOKIE)
    await tma.get("/tma")
    assert (await sign_in(tma, x)).status_code == 200
    second = tma.client.cookies.get(TMA_COOKIE)
    assert first != second
    assert await tma.auth.session_for(first) is None  # rotated out
    assert await tma.auth.session_for(second) is not None
    assert await tma.auth.active_sessions() == 1


async def test_another_signed_user_ends_the_previous_session_of_this_context(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    await tma.get("/tma")  # the reopen
    foreign = await sign_in(tma, x, user_id=OWNER + 1)
    assert foreign.status_code == 403
    header = cookies_set(foreign)[TMA_COOKIE]
    assert "Max-Age=0" in header
    assert "Partitioned" in header
    assert await tma.auth.session_for(old) is None
    after = await tma.get("/payments")  # the old cookie no longer opens anything
    assert after.status_code == 303
    assert location(after).startswith("/tma")
    assert (await tma.get("/tma/session")).status_code == 401


async def test_a_signed_foreign_user_with_a_stale_window_is_still_foreign(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    response = await sign_in(tma, x, user_id=OWNER + 1, age=timedelta(hours=1))
    assert response.status_code == 403
    assert await tma.auth.session_for(old) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"token": "999999:OTHER", "user_id": OWNER + 1},  # not signed by Telegram for this bot
        {"token": "999999:OTHER"},
    ],
)
async def test_unsigned_garbage_does_not_end_the_session(tma, x, kwargs):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    response = await sign_in(tma, x, **kwargs)
    assert response.status_code == 401
    assert cookies_set(response) == {}
    assert await tma.auth.session_for(old) is not None
    garbage = await tma.client.post(
        "/tma/auth", content=b"{bad", headers={"content-type": "application/json"}
    )
    assert garbage.status_code == 401
    assert (await tma.get("/payments")).status_code == 200


async def test_stale_owner_data_does_not_end_the_session_either(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    assert (await sign_in(tma, x, age=timedelta(hours=1))).status_code == 401
    assert await tma.auth.session_for(old) is not None


async def test_a_foreign_user_without_a_session_touches_no_other_session(tma, x):
    await tma.login()  # an ordinary session, no Mini App cookie
    web_token = tma.client.cookies.get("xboct_session")
    response = await sign_in(tma, x, user_id=OWNER + 1)
    assert response.status_code == 403
    assert await tma.auth.session_for(web_token) is not None


async def test_the_entry_page_after_logout_has_no_auto_sign_in(tma):
    page = await tma.get("/tma?bye=1")
    assert "Сессия завершена" in page.text
    assert "data-state" not in page.text


async def test_session_check_needs_a_mini_app_session(tma, x):
    assert (await tma.get("/tma/session")).status_code == 401
    await tma.login()  # an ordinary session is not a Mini App session
    assert (await tma.get("/tma/session")).status_code == 401


# --- CSRF, origin, and two cookies at once -----------------------------------------------


async def test_mutations_still_need_csrf_and_our_own_origin(tma, x):
    await sign_in(tma, x)
    html = await tma.page("/tasks/new")
    form = hidden(html, "/tasks")
    ok = await tma.post("/tasks", {**form, "title": "Из Mini App"})
    assert ok.status_code == 303
    no_csrf = await tma.post("/tasks", {"idem": "0" * 32, "title": "Без токена"})
    assert no_csrf.status_code == 403
    for origin in ("https://web.telegram.org", "https://evil.example"):
        foreign = await tma.post(
            "/tasks",
            {**hidden(html, "/tasks"), "idem": "1" * 32, "title": "Чужой origin"},
            headers={"origin": origin},
        )
        assert foreign.status_code == 403


async def test_with_both_cookies_one_session_decides_everything(tma, x):
    await tma.login()
    web_token = tma.client.cookies.get("xboct_session")
    await sign_in(tma, x)
    page = await tma.page("/tasks/new")
    assert '<body class="tma">' in page  # the Mini App session wins
    form = hidden(page, "/tasks")
    assert form["csrf"] == csrf_token(tma.client.cookies.get(TMA_COOKIE))
    assert form["csrf"] != csrf_token(web_token)
    wrong = await tma.post(
        "/tasks", {**form, "csrf": csrf_token(web_token), "idem": "2" * 32, "title": "x"}
    )
    assert wrong.status_code == 403  # the other cookie's token is not accepted
    right = await tma.post("/tasks", {**form, "idem": "3" * 32, "title": "Верный"})
    assert right.status_code == 303


async def test_logout_ends_only_the_session_it_used_and_deletes_its_cookie(tma, x):
    await tma.login()
    web_token = tma.client.cookies.get("xboct_session")
    await sign_in(tma, x)
    html = await tma.page("/settings")
    response = await tma.post("/logout", hidden(html, "/logout"))
    assert response.status_code == 303
    assert location(response) == "/tma?bye=1"
    header = cookies_set(response)[TMA_COOKIE]
    assert "Max-Age=0" in header
    for attribute in ("Secure", "HttpOnly", "SameSite=None", "Partitioned", "Path=/"):
        assert attribute in header  # deleted with the attributes it was set with
    assert "xboct_session" not in cookies_set(response)
    assert await tma.auth.session_for(web_token) is not None
    assert (await tma.get("/tma/session")).status_code == 401


async def test_ordinary_logout_leaves_the_mini_app_cookie_alone(tma, x):
    await sign_in(tma, x)
    tma.client.cookies.delete(TMA_COOKIE)
    await tma.login()
    html = await tma.page("/settings")
    response = await tma.post("/logout", hidden(html, "/logout"))
    assert location(response) == "/login"
    assert TMA_COOKIE not in cookies_set(response)


async def test_revoke_all_covers_both_ways_of_signing_in(tma, x):
    await tma.login()
    await sign_in(tma, x)
    html = await tma.page("/settings")
    response = await tma.post("/sessions/revoke-all", hidden(html, "/sessions/revoke-all"))
    assert location(response) == "/tma?bye=1"
    names = cookies_set(response)
    assert "Max-Age=0" in names[TMA_COOKIE]
    assert "Partitioned" in names[TMA_COOKIE]
    assert "xboct_session" in names
    assert await tma.auth.active_sessions() == 0


async def test_a_session_of_the_wrong_kind_in_the_wrong_cookie_is_still_a_session(tma, x):
    # Both cookies hold ordinary server-side sessions; which cookie carried the token
    # chooses the mode. A random token in the Mini App cookie is nothing.
    await tma.login()
    tma.client.cookies.set(TMA_COOKIE, "not-a-session", domain="testserver.local")
    page = await tma.page("/")
    assert '<body class="tma">' not in page  # falls back to the ordinary session


# --- the session cap is atomic -----------------------------------------------------


async def test_two_parallel_sign_ins_on_the_last_free_slot_open_one_session(tma, x, monkeypatch):
    for _ in range(MAX_SESSIONS_PER_WINDOW - 1):
        await tma.auth.open_tma_session("203.0.113.5")
    barrier = asyncio.Barrier(2)
    original = AuthService.tma_throttled

    async def gated(self, ip):
        result = await original(self, ip)  # both pass the early check ...
        await barrier.wait()  # ... before either has created its session
        return result

    monkeypatch.setattr(AuthService, "tma_throttled", gated)
    first, second = await asyncio.gather(sign_in(tma, x), sign_in(tma, x))
    assert sorted([first.status_code, second.status_code]) == [200, 429]
    late = first if first.status_code == 429 else second
    assert late.json() == {"ok": False, "error": "throttled"}
    assert len(await sessions(x)) == MAX_SESSIONS_PER_WINDOW


async def test_open_session_refuses_when_the_cap_is_reached_inside_its_own_transaction(tma):
    for _ in range(MAX_SESSIONS_PER_WINDOW):
        await tma.auth.open_tma_session("203.0.113.5")
    with pytest.raises(LoginThrottled):
        await tma.auth.open_tma_session("203.0.113.5")


# --- the way out to the ordinary web -------------------------------------------------


async def test_fallback_links_open_outside_the_mini_app(tma):
    page = (await tma.get("/tma")).text
    links = re.findall(r'<a [^>]*href="/login"[^>]*>', page)
    assert len(links) >= 3  # no-telegram, refused, cookies
    for link in links:
        assert 'target="_blank"' in link
        assert "noopener" in link
        assert "data-external" in link
    # and the ordinary login itself is not weakened:
    login = await tma.get("/login")
    assert login.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]


def test_the_adapter_hands_marked_links_to_telegram():
    source = (Path(tma_module.__file__).parent / "static" / "tma.js").read_text()
    assert "data-external" in source
    assert "openLink" in source


# --- another Telegram account: no fallback, no dependence on the issuing limit ------------


async def fill_quota(tma, x) -> None:
    """Sessions up to the window's cap (the one signed in before counts)."""
    used = len(await sessions(x))
    for _ in range(MAX_SESSIONS_PER_WINDOW - used):
        await tma.auth.open_tma_session("203.0.113.5")
    assert len(await sessions(x)) == MAX_SESSIONS_PER_WINDOW


async def test_after_a_foreign_identity_the_ordinary_cookie_is_no_fallback_in_the_mini_app(tma, x):
    await tma.login()  # an ordinary, independent session
    web_token = tma.client.cookies.get("xboct_session")
    await sign_in(tma, x)
    assert (await tma.get("/payments")).status_code == 200
    assert (await sign_in(tma, x, user_id=OWNER + 1)).status_code == 403
    after = await tma.get("/payments")  # same context: nothing opens through the other cookie
    assert after.status_code == 303
    assert location(after).startswith("/tma")
    assert (await tma.get("/")).status_code == 303
    post = await tma.post("/tasks", {"csrf": csrf_token(web_token), "idem": "4" * 32, "title": "x"})
    assert post.status_code in (401, 403)
    assert (await tma.get("/tma/session")).status_code == 401
    # The ordinary session was not revoked globally ...
    assert await tma.auth.session_for(web_token) is not None
    # ... and the explicit ordinary sign-in by code keeps working, outside the Mini App context.
    assert (await tma.get("/login")).status_code == 200
    await tma.login()
    assert (await tma.get("/payments")).status_code == 200


async def test_the_mini_app_context_only_accepts_its_own_session(tma, x):
    await tma.login()
    await sign_in(tma, x)
    await tma.auth.revoke_all()  # nothing valid is left in the database
    await tma.login()  # a fresh ordinary session, while the context still says Mini App
    assert (await tma.get("/payments")).status_code == 200  # the ordinary login cleared the marker


async def test_a_foreign_identity_ends_access_even_when_the_issuing_limit_is_spent(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    await fill_quota(tma, x)
    foreign = await sign_in(tma, x, user_id=OWNER + 1)
    assert foreign.status_code == 403  # the identity check ran, not the limit
    assert "Max-Age=0" in cookies_set(foreign)[TMA_COOKIE]
    assert await tma.auth.session_for(old) is None
    assert (await tma.get("/payments")).status_code == 303


async def test_a_foreign_identity_ends_access_when_failures_are_throttled_too(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    for _ in range(MAX_FAILURES_PER_IP):
        await sign_in(tma, x, token="999999:OTHER")
    assert await tma.auth.session_for(old) is not None  # garbage revoked nothing
    assert (await sign_in(tma, x, user_id=OWNER + 1)).status_code == 403
    assert await tma.auth.session_for(old) is None


async def test_the_owner_cannot_exceed_the_quota(tma, x):
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    await fill_quota(tma, x)
    response = await sign_in(tma, x)
    assert response.status_code == 429
    assert response.json() == {"ok": False, "error": "throttled"}
    assert len(await sessions(x)) == MAX_SESSIONS_PER_WINDOW
    assert all("Max-Age=0" in h for h in cookies_set(response).values())  # nothing was granted
    assert await tma.auth.session_for(old) is not None  # and the old one was not touched


async def test_unsigned_garbage_revokes_nothing_at_the_limit_either(tma, x):
    await tma.login()
    web_token = tma.client.cookies.get("xboct_session")
    await sign_in(tma, x)
    old = tma.client.cookies.get(TMA_COOKIE)
    await fill_quota(tma, x)
    for kwargs in ({"token": "999999:OTHER", "user_id": OWNER + 1}, {"token": "999999:OTHER"}):
        assert (await sign_in(tma, x, **kwargs)).status_code in (401, 429)
    junk = await tma.client.post(
        "/tma/auth", content=b"{bad", headers={"content-type": "application/json"}
    )
    assert junk.status_code in (401, 429)
    assert await tma.auth.session_for(old) is not None
    assert await tma.auth.session_for(web_token) is not None
    assert (await tma.get("/payments")).status_code == 200


async def test_a_foreign_identity_leaves_the_independent_ordinary_session_alone(tma, x):
    await tma.login()
    web_token = tma.client.cookies.get("xboct_session")
    await sign_in(tma, x)
    await fill_quota(tma, x)
    await sign_in(tma, x, user_id=OWNER + 1)
    assert await tma.auth.session_for(web_token) is not None
