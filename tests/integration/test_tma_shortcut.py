"""«Добавить на главный экран»: only in the Mini App, only through the official API.

The behaviour with a simulated SDK (supported, unsupported, added, unknown, a call only on
a click) is checked in a real browser by scripts/tma_qa.py; here: what the server renders
and what the script is allowed to contain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.test_tma_web import OWNER, TOKEN, sign_in, signed  # noqa: F401
from tests.integration.web import Web, text_of
from xboct.web import app as web_app
from xboct.web.tma import TmaConfig

# «ты»: no «пожалуйста», no «вы/вам/ваш», no plural imperatives (-ите/-йте/-ьте).
FORMAL = re.compile(
    r"пожалуйста|(?<![а-яё])(?:вы|вам|вас|ваш[а-яё]*)(?![а-яё])|[а-яё]{2,}(?:ите|йте|ьте)(?![а-яё])",
    re.I,
)
STATIC = Path(web_app.__file__).parent / "static"
JS = (STATIC / "tma-shortcut.js").read_text()


@pytest.fixture
async def tma(x):
    web = Web(x, secure_cookies=True, tma=TmaConfig(TOKEN, OWNER))
    yield web
    await web.close()


async def test_the_section_exists_only_in_the_mini_app(tma, x):
    ordinary = await tma.login()
    assert ordinary.status_code == 303
    plain = (await tma.get("/settings")).text
    assert "data-home-screen" not in plain
    assert "tma-shortcut.js" not in plain
    assert "Добавить на главный экран" not in plain

    await tma.auth.revoke_all()
    tma.client.cookies.clear()
    assert (await sign_in(tma, x)).status_code == 200
    page = (await tma.get("/settings")).text
    assert re.search(
        r"<section[^>]*data-home-screen[^>]*hidden", page
    )  # hidden until the SDK answers
    assert 'src="/static/tma-shortcut.js"' in page
    assert "<script>" not in page
    assert text_of(page).count("Добавить на главный экран") == 1


async def test_nothing_is_offered_by_the_server_alone(tma, x):
    await sign_in(tma, x)
    page = (await tma.get("/settings")).text
    # every state text and the button start hidden; the script reveals what applies
    for name in ("offer", "unknown", "added", "requested", "error"):
        assert re.search(rf'<p data-state="{name}" hidden>', page)
    assert re.search(r'<button[^>]*data-action="add-home-screen"[^>]*hidden', page)


async def test_the_wording_is_in_style_and_never_claims_what_it_does_not_know(tma, x):
    await sign_in(tma, x)
    page = (await tma.get("/settings")).text
    texts = dict(re.findall(r'<p data-state="(\w+)" hidden>([^<]*)</p>', page))
    assert set(texts) == {"offer", "unknown", "added", "requested", "error"}
    for text in texts.values():
        assert not FORMAL.search(text)  # «ты», no «пожалуйста»
    assert texts["added"] == "Ярлык уже добавлен."
    # unknown: does not say the shortcut is missing; requested: does not say it succeeded
    assert "нет" not in texts["unknown"].lower().replace("не сообщает", "")
    assert not re.search(r"добавлен(?![а-яё])", texts["unknown"])
    assert not re.search(r"добавлен(?![а-яё])", texts["requested"])
    assert "может не сообщить" in texts["requested"]
    assert texts["error"] == "Не удалось запросить ярлык. Попробуй ещё раз."
    assert not re.search(r"добавлен(?![а-яё])", texts["error"])


def test_only_the_official_api_with_a_version_and_method_check():
    assert 'isVersionAtLeast("8.0")' in JS
    assert 'typeof tg.checkHomeScreenStatus !== "function"' in JS
    assert 'typeof tg.addToHomeScreen !== "function"' in JS
    assert "homeScreenAdded" in JS
    assert "homeScreenChecked" in JS
    for status in ("added", "missed", "unknown", "unsupported"):
        assert f'"{status}"' in JS


def test_the_shortcut_is_requested_only_inside_the_click_handler():
    assert JS.count("addToHomeScreen()") == 1
    start = JS.index('button.addEventListener("click"')
    end = JS.index("});", start)
    assert "tg.addToHomeScreen()" in JS[start:end]
    # and nothing reports success because the method was called
    handler = JS[start:end]
    assert 'show("added"' not in handler


def test_no_pwa_service_worker_manifest_or_new_sign_in_mechanism():
    names = {p.name.lower() for p in STATIC.rglob("*")}
    assert not any("manifest" in n or n in ("sw.js", "service-worker.js") for n in names)
    for path in [*STATIC.glob("*.js"), *(STATIC.parent / "templates").glob("*.html")]:
        text = path.read_text()
        assert "serviceWorker" not in text
        assert 'rel="manifest"' not in text
        assert "localStorage" not in text
        assert "openTelegramLink" not in text
    assert "token" not in JS.lower()


def test_a_confirmed_added_is_final_and_stale_answers_are_ignored():
    # the guards are in the script; the browser regressions in scripts/tma_qa.py run them
    assert "if (confirmed) { return; }" in JS
    assert "if (n < applied) { return; }" in JS
    assert "if (!answered && !confirmed)" in JS  # the fallback timer never overwrites «added»
    assert 'tg.onEvent("homeScreenAdded", confirm)' in JS


def test_an_exception_shows_a_neutral_error_and_never_retries_by_itself():
    start = JS.index('button.addEventListener("click"')
    end = JS.index("});", start)
    handler = JS[start:end]
    assert 'show("error", true)' in handler
    assert handler.count("addToHomeScreen()") == 1
    assert "setTimeout(check" in handler  # only the status check is repeated, not the request
    assert "setTimeout(function () { tg.addToHomeScreen" not in JS
    assert "setInterval" not in JS
