"""The simplified home screen: what needs attention first, nothing shown twice.

Dates and overdue-ness are the agenda's (unchanged); subscriptions and VPS come from the
payments read model; thresholds are the stored settings, not new constants.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from tests.integration.test_web_flows import web  # noqa: F401  (fixture)
from tests.integration.web import hidden, text_of
from xboct.application import commands as c
from xboct.storage.models import Task
from xboct.web import views

TODAY = date(2026, 10, 8)  # the test clock


async def home(x) -> views.Home:
    return await x.app.read(views.home_view)


def keys(block: list[views.HomeLine]) -> list[str]:
    return [line.key for line in block]


def titles(block: list[views.HomeLine]) -> list[str]:
    return [line.title for line in block]


def all_lines(h: views.Home) -> list[views.HomeLine]:
    return [*h.attention, *h.today, *h.week, *h.overview]


async def task(x, title: str, due: date | None = None) -> str:
    result = await x.ok(c.CreateTask(title=title, due_date=due))
    return result.data["task_id"]


async def coverage(x, title: str, until: date) -> str:
    result = await x.ok(c.CreateObligation(title=title, kind="coverage", coverage_until=until))
    return result.data["obligation_id"]


async def vps(x, title: str, *, rate: int | None, topup: int = 0, zero: bool = True) -> str:
    start = c.BalanceStart(assume_zero=zero, rate_minor_per_day=rate) if zero else None
    result = await x.ok(c.CreateObligation(title=title, kind="balance", balance=start))
    if topup:
        await x.ok(c.RecordTopup(obligation_id=result.data["obligation_id"], amount_minor=topup))
    return result.data["obligation_id"]


async def rent(x, title: str, anchor: date) -> str:
    result = await x.ok(
        c.CreateObligation(
            title=title,
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=anchor),
        )
    )
    return result.data["obligation_id"]


async def _version(uow) -> int:
    return uow.settings.version


async def settings(x, **fields: int) -> None:
    version = await x.app.read(lambda uow: _version(uow))
    await x.ok(c.UpdateSettings(expected_version=version, **fields))


# --- layout and duplicates ------------------------------------------------------------


async def test_blocks_hold_the_right_records_each_once(x):
    await task(x, "Оплатить штраф", TODAY - timedelta(days=2))
    await task(x, "Позвонить маме", TODAY)
    await task(x, "Записаться к врачу", TODAY + timedelta(days=3))
    await task(x, "Разобрать полку")  # undated
    await vps(x, "VPS Германия", rate=4_000, topup=20_000)  # 5 days: low
    await vps(x, "VPS Нидерланды", rate=1_000, topup=40_000)  # 40 days: fine
    await coverage(x, "Музыка", TODAY + timedelta(days=34))
    await rent(x, "Аренда", TODAY + timedelta(days=60))  # far away: not on the home screen

    h = await home(x)
    assert titles(h.attention) == ["Оплатить штраф", "VPS Германия"]
    assert titles(h.today) == ["Позвонить маме"]
    assert titles(h.week) == ["Записаться к врачу"]
    assert titles(h.overview) == ["VPS Нидерланды", "Музыка"]
    assert h.undated == 1
    assert "Аренда" not in titles(all_lines(h))
    assert len(keys(all_lines(h))) == len(set(keys(all_lines(h))))  # nothing shown twice


async def test_a_subscription_in_attention_is_not_repeated_in_the_overview(x):
    await coverage(x, "Домен", TODAY + timedelta(days=2))  # last paid day is tomorrow
    h = await home(x)
    assert titles(h.attention) == ["Домен"]
    assert h.overview == []
    assert h.week == []  # nor in the 7-day list, although the agenda has it there


async def test_overdue_is_not_repeated_in_today_or_week(x):
    await task(x, "Старое", TODAY - timedelta(days=1))
    h = await home(x)
    assert titles(h.attention) == ["Старое"]
    assert h.today == []
    assert h.week == []


async def test_all_open_periods_of_one_payment_stay_separate(x):
    await rent(x, "Аренда", date(2026, 8, 5))  # Aug, Sep, Oct are past due on 8 Oct
    h = await home(x)
    periods = [line for line in h.attention if line.title == "Аренда"]
    assert len(periods) == 3
    assert len(set(keys(periods))) == 3
    assert all("срок прошёл, отметки оплаты нет" in line.meta for line in periods)
    assert any("август 2026" in line.meta for line in periods)


async def test_due_today_alone_is_not_overdue(x):
    await task(x, "Сегодня без времени", TODAY)
    h = await home(x)
    assert h.attention == []
    assert titles(h.today) == ["Сегодня без времени"]
    assert "срок прошёл" not in h.today[0].meta


async def test_wording_of_overdue_tasks_and_payments(x):
    await task(x, "Дело", TODAY - timedelta(days=1))
    await rent(x, "Квартира", date(2026, 10, 5))
    h = await home(x)
    by = {line.title: line for line in h.attention}
    assert "срок прошёл, выполнение не отмечено" in by["Дело"].meta
    assert "срок прошёл, отметки оплаты нет" in by["Квартира"].meta
    assert "отключ" not in " ".join(line.meta for line in h.attention)


# --- subscriptions: the stored reminder rule, the inclusive last paid day ----------------


@pytest.mark.parametrize(
    ("advance", "until_offset", "in_attention"),
    [
        (3, 3, True),  # notice day: coverage_until - 3 == today
        (3, 4, False),  # one day before the notice
        (7, 7, True),
        (7, 8, False),
        (1, 1, True),  # advance 1 -> only the last paid day itself speaks
        (1, 2, False),
        (0, 1, True),
        (0, 2, False),
    ],
)
async def test_subscription_boundary_follows_the_advance_setting(
    x, advance, until_offset, in_attention
):
    await settings(x, payment_advance_days=advance)
    await coverage(x, "Сервис", TODAY + timedelta(days=until_offset))
    h = await home(x)
    assert (titles(h.attention) == ["Сервис"]) is in_attention
    assert (titles(h.overview) == ["Сервис"]) is (not in_attention)


async def test_last_paid_day_is_shown_inclusive(x):
    await coverage(x, "Сервис", TODAY + timedelta(days=20))  # first unpaid day: 28 Oct
    [line] = (await home(x)).overview
    assert "оплачено по 27 октября (вт) включительно" in line.meta
    assert "28 октября" not in line.meta


async def test_expired_subscription_says_so(x):
    await coverage(x, "Сервис", TODAY)  # first unpaid day is today: last paid day was yesterday
    [line] = (await home(x)).attention
    assert line.meta.startswith("оплата истекла: последний оплаченный день — 7 октября")
    assert line.action == ("Продлить…", line.href)


async def test_today_is_the_last_paid_day_not_expired(x):
    await coverage(x, "Сервис", TODAY + timedelta(days=1))
    [line] = (await home(x)).attention
    assert "оплата истекла" not in line.meta
    assert "оплачено по 8 октября (чт) включительно; пора продлить" in line.meta


# --- VPS -----------------------------------------------------------------------------


async def test_vps_shows_days_without_money_or_instants(x):
    await vps(x, "VPS", rate=4_000, topup=20_000)
    [line] = (await home(x)).attention
    assert line.meta == "примерно 5 дней, пора пополнить"
    assert line.estimate
    assert "₽" not in line.meta
    assert ":" not in line.meta
    assert "₽" in (line.details or "")  # the full text lives in the details


async def test_vps_under_a_day(x):
    await vps(x, "VPS", rate=4_000, topup=3_000)
    [line] = (await home(x)).attention
    assert line.meta == "меньше суток, пора пополнить"


async def test_vps_depleted_is_estimated_not_switched_off(x):
    await vps(x, "VPS", rate=4_000, topup=8_000)
    x.clock.advance(days=5)
    [line] = (await home(x)).attention
    assert line.meta == "расчётный запас исчерпан — пора пополнить"
    assert "отключ" not in line.meta


async def test_vps_threshold_is_the_stored_setting(x):
    await vps(x, "VPS", rate=1_000, topup=8_000)  # 8 days
    assert titles((await home(x)).overview) == ["VPS"]  # default threshold: 5
    await settings(x, balance_threshold_days=10)
    h = await home(x)
    assert titles(h.attention) == ["VPS"]
    assert h.overview == []


async def test_vps_zero_rate_is_stopped_without_a_refill_date(x):
    await vps(x, "Старый VPS", rate=0, topup=50_000)
    h = await home(x)
    assert h.attention == []
    [line] = h.overview
    assert line.meta == "списание остановлено (расход 0)"
    assert "пополнить" not in line.meta
    assert not line.estimate


async def test_unknown_balance_is_not_called_all_good(x, web):  # noqa: F811
    await x.ok(c.CreateObligation(title="Новый VPS", kind="balance"))
    h = await home(x)
    assert h.attention == []
    assert h.calm
    [line] = h.overview
    assert "нет стартового остатка" in line.meta
    assert h.missing == ["Новый VPS"]
    page = text_of(await web.page("/"))
    assert "Срочных действий по записанным данным нет." in page
    assert "данных не хватает: Новый VPS" in page


async def test_rate_not_set_asks_for_the_missing_value(x):
    await vps(x, "VPS без расхода", rate=None, topup=10_000)
    h = await home(x)
    [line] = h.overview
    assert line.meta == "расход в день не задан — запас не оценить"
    assert h.missing == ["VPS без расхода"]
    assert h.attention == []


async def test_calm_screen_without_unknowns(x, web):  # noqa: F811
    await vps(x, "VPS", rate=1_000, topup=40_000)
    h = await home(x)
    assert h.calm
    assert h.missing == []
    page = text_of(await web.page("/"))
    assert "Срочных действий по записанным данным нет." in page
    assert "данных не хватает" not in page
    assert "примерно 40 дней" in page


# --- the page: links, buttons, forms, quick add ----------------------------------------


async def test_actions_are_links_to_cards_or_existing_guarded_forms(x, web):  # noqa: F811
    ids = {
        "task": await task(x, "Дело", TODAY - timedelta(days=1)),
        "rent": await rent(x, "Квартира", date(2026, 10, 5)),
        "vps": await vps(x, "VPS", rate=4_000, topup=20_000),
        "sub": await coverage(x, "Сервис", TODAY + timedelta(days=2)),
    }
    html = await web.page("/")
    page = text_of(html)
    for label in ("Оплатить…", "Пополнить…", "Продлить…"):
        assert label in page
    for oid in (ids["rent"], ids["vps"], ids["sub"]):
        assert f'href="/obligations/{oid}"' in html
    # No silent payment mark and no silent extension from the home screen.
    assert f"/obligations/{ids['rent']}/pay" not in html
    assert "/extend" not in html
    assert "/topup" not in html
    # Task buttons are the existing forms: csrf, idempotency key, version.
    fields = hidden(html, f"/tasks/{ids['task']}/complete")
    assert fields["csrf"]
    assert fields["idem"]
    assert fields["version"] == "1"


async def test_week_block_has_links_only(x, web):  # noqa: F811
    tid = await task(x, "Через пару дней", TODAY + timedelta(days=2))
    html = await web.page("/")
    assert f'href="/tasks/{tid}"' in html
    assert f"/tasks/{tid}/complete" not in html


async def test_navigation_says_home_and_url_is_unchanged(web):  # noqa: F811
    html = await web.page("/")
    assert '<a href="/" aria-current="page">Главная</a>' in html
    assert ">Сегодня</a>" not in html


async def test_quick_add_creates_a_task_without_a_due_date(x, web):  # noqa: F811
    html = await web.page("/")
    data = {**hidden(html, "/tasks"), "title": "завтра позвонить маме"}
    response = await web.post("/tasks", data)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/")
    [created] = await x.all(Task, Task.title == "завтра позвонить маме")
    assert created.due_date is None
    assert created.due_at is None  # nothing was "recognised"
    page = text_of(await web.page("/"))
    assert "Без срока: 1" in page
    assert "дата из текста не распознаётся" in page


async def test_quick_add_form_has_only_a_title_and_the_link_does_not_submit(web):  # noqa: F811
    html = await web.page("/")
    assert 'name="due_date"' not in html
    assert '<a href="/tasks/new">полной форме</a>' in html


async def test_full_form_still_sets_a_due_date(x, web):  # noqa: F811
    html = await web.page("/tasks/new")
    data = {**hidden(html, "/tasks"), "title": "С датой", "due_date": "2026-10-20"}
    assert (await web.post("/tasks", data)).status_code == 303
    [created] = await x.all(Task, Task.title == "С датой")
    assert created.due_date == date(2026, 10, 20)


async def test_long_titles_are_wrapped_by_the_stylesheet(web):  # noqa: F811
    css = (await web.get("/static/app.css")).text
    assert "overflow-wrap: anywhere" in css
    assert "flex: 1 1 14rem" in css
    assert "min-width: 0" in css


async def test_details_toggle_has_a_phone_sized_tap_target(web):  # noqa: F811
    css = (await web.get("/static/app.css")).text
    block = css.split("details.more > summary {", 1)[1].split("}", 1)[0]
    assert "min-height: 44px" in block


async def test_calm_overview_actions_are_neutral_urgent_ones_stay_primary(x, web):  # noqa: F811
    await vps(x, "VPS Германия", rate=4_000, topup=20_000)  # 5 days: attention
    await vps(x, "VPS Нидерланды", rate=1_000, topup=40_000)  # fine: calm overview
    await coverage(x, "Музыка", TODAY + timedelta(days=34))  # calm overview
    await coverage(x, "Домен", TODAY + timedelta(days=2))  # attention
    html = await web.page("/")
    attention, _, rest = html.partition('class="card section-overview"')
    assert 'class="btn primary" href' in attention
    assert re.search(r'class="btn primary"[^>]*>Пополнить…<', attention)
    assert re.search(r'class="btn primary"[^>]*>Продлить…<', attention)
    assert ">Продлить…<" in rest
    assert ">Открыть…<" in rest
    assert not re.search(r'class="btn primary"[^>]*>(Продлить…|Открыть…|Пополнить…)<', rest)
