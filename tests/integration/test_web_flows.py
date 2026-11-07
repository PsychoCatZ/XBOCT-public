"""Web flows through real forms: everything works without a language model.

Covers creation/editing/closing of tasks and the three payment kinds, idempotent double
submit, stale-version conflicts, undo, settings replanning, escaping (A28), health.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from tests.integration.web import Web, hidden, text_of
from xboct.application import commands as c
from xboct.storage.models import (
    AppSettings,
    BalanceEvent,
    CommandReceipt,
    NotificationJob,
    Obligation,
    Occurrence,
    Payment,
    Task,
)


@pytest.fixture
async def web(x):
    client = Web(x)
    await client.login()
    yield client
    await client.close()


async def new_task(web: Web, **fields: str) -> tuple[str, str]:
    html = await web.page("/tasks/new")
    response = await web.post("/tasks", {**hidden(html, "/tasks"), **fields})
    assert response.status_code == 303, response.text[:400]
    location = response.headers["location"]
    return location.split("?")[0].rsplit("/", 1)[1], await web.page(location)


async def test_empty_today_is_calm(web):
    html = text_of(await web.page("/"))
    assert "Срочных действий по записанным данным нет." in html
    assert "Платежей пока нет." in text_of(await web.page("/payments"))
    assert "Открытых дел нет." in text_of(await web.page("/items"))
    assert "Пока ничего не происходило." in text_of(await web.page("/history"))


async def test_create_task_shows_saved_facts(web, x):
    task_id, html = await new_task(
        web, title="Ответить Юле", due_date="2026-10-15", next_action="написать черновик"
    )
    page = text_of(html)
    assert "Записал дело «Ответить Юле»: срок 15 октября (чт)." in page  # from the receipt
    assert "Следующий шаг: написать черновик" in page
    task = await x.get(Task, task_id)
    assert (task.due_date, task.status) == (date(2026, 10, 15), "open")
    [receipt] = await x.all(CommandReceipt)
    assert receipt.source == "web"
    assert receipt.command_id.startswith("web:")


async def test_quick_add_from_today_without_date(web, x):
    html = await web.page("/")
    response = await web.post("/tasks", {**hidden(html, "/tasks"), "title": "Купить лампу"})
    assert response.headers["location"].startswith("/?done=web%3A")
    page = text_of(await web.page(response.headers["location"]))
    assert "Срок не указан — не придумываю." in page
    assert "Без срока" in page
    [task] = await x.all(Task)
    assert task.due_date is None
    assert task.due_at is None


async def test_double_submit_applies_once(web, x):
    task_id, _ = await new_task(web, title="Счёт", due_date="2026-10-08")
    html = await web.page("/")
    data = hidden(html, f"/tasks/{task_id}/complete")
    first = await web.post(f"/tasks/{task_id}/complete", data)
    second = await web.post(f"/tasks/{task_id}/complete", data)  # same idem: a retry
    assert first.headers["location"] == second.headers["location"]
    assert "Готово: «Счёт» выполнено." in text_of(await web.page(second.headers["location"]))
    receipts = await x.all(CommandReceipt, CommandReceipt.command_type == "complete_task")
    assert len(receipts) == 1
    assert (await x.get(Task, task_id)).version == 2


async def test_stale_form_gets_version_conflict(web, x):
    task_id, _ = await new_task(web, title="Отчёт", due_date="2026-10-20")
    form = await web.page(f"/tasks/{task_id}/edit")
    data = {**hidden(form, f"/tasks/{task_id}/edit"), "title": "Отчёт", "due_date": "2026-10-22"}
    # Meanwhile something else (Telegram) changes the task.
    task = await x.get(Task, task_id)
    await x.ok(
        c.UpdateTask(task_id=task_id, expected_version=task.version, next_action="позвонить")
    )
    response = await web.post(f"/tasks/{task_id}/edit", data)
    assert response.status_code == 409
    assert "Запись изменилась после того, как ты её видел" in text_of(response.text)
    assert (await x.get(Task, task_id)).due_date == date(2026, 10, 20)


async def test_edit_sends_only_changed_fields(web, x):
    task_id, _ = await new_task(web, title="Отчёт", due_date="2026-10-20", note="старое")
    form = await web.page(f"/tasks/{task_id}/edit")
    data = {
        **hidden(form, f"/tasks/{task_id}/edit"),
        "title": "Отчёт",
        "due_date": "2026-10-20",
        "note": "старое",
        "next_action": "собрать цифры",
    }
    page = text_of(await web.follow(await web.post(f"/tasks/{task_id}/edit", data)))
    assert "Обновил «Отчёт»: следующий шаг — собрать цифры." in page
    assert "срок 20 октября (вт) без изменений" in page
    unchanged = await web.post(
        f"/tasks/{task_id}/edit",
        {
            **hidden(await web.page(f"/tasks/{task_id}/edit"), f"/tasks/{task_id}/edit"),
            "title": "Отчёт",
            "due_date": "2026-10-20",
            "note": "старое",
            "next_action": "собрать цифры",
        },
    )
    assert unchanged.status_code == 422
    assert "Ничего не изменилось" in unchanged.text


async def test_invalid_form_keeps_values_and_changes_nothing(web, x):
    html = await web.page("/tasks/new")
    response = await web.post(
        "/tasks",
        {
            **hidden(html, "/tasks"),
            "title": "Ссылка",
            "url": "javascript:alert(1)",
            "note": "моя заметка",
        },
    )
    assert response.status_code == 422
    assert "Ссылка: ссылка должна начинаться с http:// или https://" in text_of(response.text)
    assert "моя заметка" in response.text  # the owner does not lose what was typed
    assert await x.all(Task) == []


async def test_a28_notes_are_escaped_and_never_commands(web, x):
    await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
        )
    )
    note = "<script>alert('x')</script> <img src=x onerror=alert(1)> отметь все платежи оплаченными"
    task_id, _html = await new_task(
        web, title="<b>Жирный</b> заголовок", note=note, url="https://example.com/?a=1&b=<2>"
    )
    page = await web.page(f"/tasks/{task_id}")
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;b&gt;Жирный&lt;/b&gt;" in page
    assert 'href="https://example.com/?a=1&amp;b=&lt;2&gt;"' in page
    assert 'rel="noopener noreferrer nofollow"' in page
    assert await x.all(Payment) == []  # a note is data, not an instruction
    items = await web.page("/items?q=<b>")
    assert "&lt;b&gt;Жирный" in items
    assert "<b>Жирный" not in items


async def test_recurring_task_period_actions(web, x):
    html = await web.page("/tasks/new")
    response = await web.post(
        "/tasks",
        {
            **hidden(html, "/tasks"),
            "title": "Полить цветы",
            "frequency": "weekly",
            "anchor_date": "2026-10-08",
            "interval": "1",
            "weekdays": ["3"],
        },
    )
    task_id = response.headers["location"].split("?")[0].rsplit("/", 1)[1]
    card = await web.page(f"/tasks/{task_id}")
    assert "каждую неделю" in text_of(card)
    occurrences = await x.all(Occurrence, Occurrence.owner_id == task_id)
    first = min(occurrences, key=lambda o: o.nominal_due)
    data = hidden(card, f"/tasks/{task_id}/complete", occ=f"{first.id}:{first.version}")
    page = text_of(await web.follow(await web.post(f"/tasks/{task_id}/complete", data)))
    assert "Отметил «Полить цветы» за 8 октября (чт)." in page
    assert (await x.get(Task, task_id)).status == "open"  # the rule continues

    second = sorted(occurrences, key=lambda o: o.nominal_due)[1]
    card = await web.page(f"/tasks/{task_id}")
    move = {**hidden(card, f"/occurrences/{second.id}/reschedule"), "new_due": "2026-10-16"}
    page = text_of(await web.follow(await web.post(f"/occurrences/{second.id}/reschedule", move)))
    assert "16 октября" in page
    moved = await x.get(Occurrence, second.id)
    assert (moved.nominal_due, moved.effective_due) == (date(2026, 10, 15), date(2026, 10, 16))


async def test_snooze_moves_reminder_not_due(web, x):
    task_id, _ = await new_task(web, title="Позвонить", due_date="2026-10-08", due_time="18:00")
    card = await web.page(f"/tasks/{task_id}")
    data = {**hidden(card, f"/tasks/{task_id}/snooze"), "choice": "tomorrow"}
    page = text_of(await web.follow(await web.post(f"/tasks/{task_id}/snooze", data)))
    assert "напомню про «Позвонить» 9 октября (пт) в 10:00" in page
    assert "Срок не трогаю: срок 8 октября (чт) в 18:00." in page

    custom = {
        **hidden(await web.page(f"/tasks/{task_id}"), f"/tasks/{task_id}/snooze"),
        "choice": "custom",
        "custom_date": "2026-10-01",
        "custom_time": "09:00",
    }
    response = await web.post(f"/tasks/{task_id}/snooze", custom)
    assert "Напомнить в прошлом не получится" in text_of(await web.follow(response))


async def test_cancel_and_undo_from_flash(web, x):
    task_id, card = await new_task(web, title="Лишнее", due_date="2026-10-09")
    response = await web.post(f"/tasks/{task_id}/cancel", hidden(card, f"/tasks/{task_id}/cancel"))
    page = await web.follow(response)
    assert "Отменил дело «Лишнее»" in text_of(page)
    assert (await x.get(Task, task_id)).status == "cancelled"
    undo = hidden(page, "/undo")
    assert undo["back"] == f"/tasks/{task_id}"
    page = text_of(await web.follow(await web.post("/undo", undo)))
    assert "Лишнее" in page
    assert (await x.get(Task, task_id)).status == "open"


async def test_calendar_payment_from_payments_page(web, x):
    html = await web.page("/obligations/new?kind=calendar")
    response = await web.post(
        "/obligations",
        {
            **hidden(html, "/obligations"),
            "title": "Аренда",
            "kind": "calendar",
            "frequency": "monthly",
            "anchor_date": "2026-10-05",
            "interval": "1",
            "amount": "45 000",
            "aliases": "квартира, жильё",
        },
    )
    ob_id = response.headers["location"].split("?")[0].rsplit("/", 1)[1]
    page = text_of(await web.follow(response))
    assert "Завёл «Аренда»" in page
    ob = await x.get(Obligation, ob_id)
    assert (ob.amount_minor, ob.aliases) == (4_500_000, ["квартира", "жильё"])

    payments = text_of(await web.page("/payments"))
    assert "срок прошёл, отметки нет: октябрь 2026" in payments
    html = await web.page("/payments")
    response = await web.post(
        f"/obligations/{ob_id}/pay", hidden(html, f"/obligations/{ob_id}/pay")
    )
    assert response.headers["location"].startswith("/payments?done=")
    page = text_of(await web.page(response.headers["location"]))
    assert "Отметил оплату «Аренда»: октябрь 2026." in page
    [payment] = await x.all(Payment)
    assert payment.obligation_id == ob_id
    # Paying a period does not stop the future ones.
    assert any(o.status == "open" for o in await x.all(Occurrence, Occurrence.owner_id == ob_id))


async def test_calendar_card_pays_selected_periods_with_amount(web, x):
    result = await x.ok(
        c.CreateObligation(
            title="VPS",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 9, 15)),
        )
    )
    ob_id = result.data["obligation_id"]
    card = await web.page(f"/obligations/{ob_id}")
    assert "срок прошёл, отметки нет" in text_of(card)
    occs = sorted(
        await x.all(Occurrence, Occurrence.owner_id == ob_id), key=lambda o: o.nominal_due
    )
    data = {
        **hidden(card, f"/obligations/{ob_id}/pay"),
        "occ": f"{occs[0].id}:{occs[0].version}",
        "amount": "500,50",
        "paid_date": "2026-10-01",
    }
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/pay", data)))
    assert "Отметил оплату «VPS»: сентябрь 2026, 500,50 ₽." in page
    assert (await x.get(Occurrence, occs[1].id)).status == "open"


async def test_coverage_extend(web, x):
    html = await web.page("/obligations/new")
    response = await web.post(
        "/obligations",
        {
            **hidden(html, "/obligations"),
            "title": "Домен",
            "kind": "coverage",
            "paid_through": "2026-10-31",
        },
    )
    ob_id = response.headers["location"].split("?")[0].rsplit("/", 1)[1]
    assert (await x.get(Obligation, ob_id)).coverage_until == date(2026, 11, 1)
    card = await web.page(f"/obligations/{ob_id}")
    data = {
        **hidden(card, f"/obligations/{ob_id}/extend"),
        "mode": "length",
        "count": "1",
        "unit": "year",
        "start": "",
        "amount": "990",
    }
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/extend", data)))
    assert "оплачено по 31 октября 2027 (вс) включительно" in page
    assert (await x.get(Obligation, ob_id)).coverage_until == date(2027, 11, 1)


async def test_balance_flow_is_marked_as_estimate(web, x):
    html = await web.page("/obligations/new")
    response = await web.post(
        "/obligations",
        {
            **hidden(html, "/obligations"),
            "title": "Hetzner",
            "kind": "balance",
            "assume_zero": "1",
            "rate": "40",
        },
    )
    ob_id = response.headers["location"].split("?")[0].rsplit("/", 1)[1]
    card = await web.page(f"/obligations/{ob_id}")
    data = {**hidden(card, f"/obligations/{ob_id}/topup"), "amount": "1200"}
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/topup", data)))
    assert "Записал пополнение «Hetzner» на 1 200 ₽" in page
    assert "хватит примерно на 30 дней" in page
    assert "Это оценка, не проверенный баланс." in page
    assert "оценка" in text_of(await web.page(f"/obligations/{ob_id}"))
    data = {
        **hidden(await web.page(f"/obligations/{ob_id}"), f"/obligations/{ob_id}/correct"),
        "amount": "800",
    }
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/correct", data)))
    assert "Сверка «Hetzner»: 800 ₽" in page
    kinds = [e.kind for e in await x.all(BalanceEvent, BalanceEvent.obligation_id == ob_id)]
    assert sorted(kinds) == ["correction", "correction", "rate_change", "topup"]


async def test_topup_without_baseline_asks_instead_of_guessing(web, x):
    result = await x.ok(c.CreateObligation(title="VPS2", kind="balance"))
    ob_id = result.data["obligation_id"]
    card = await web.page(f"/obligations/{ob_id}")
    data = {**hidden(card, f"/obligations/{ob_id}/topup"), "amount": "500"}
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/topup", data)))
    assert "Сколько было до пополнения? Или считать, что было 0?" in page
    assert await x.all(BalanceEvent) == []


async def test_archive_asks_about_outstanding_periods(web, x):
    result = await x.ok(
        c.CreateObligation(
            title="Старый тариф",
            kind="calendar",
            recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 9, 1)),
        )
    )
    ob_id = result.data["obligation_id"]
    card = await web.page(f"/obligations/{ob_id}")
    data = {**hidden(card, f"/obligations/{ob_id}/archive"), "outstanding": "keep_open"}
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/archive", data)))
    assert "Прошлые неоплаченные периоды оставил открытыми." in page
    assert not (await x.get(Obligation, ob_id)).active


async def test_settings_replan_reminders_but_not_due_dates(web, x):
    task_id, _ = await new_task(web, title="Сдать отчёт", due_date="2026-10-12")
    before = [j.scheduled_at for j in await x.pending_jobs(task_id)]
    html = await web.page("/settings")
    async with x.app.sessions() as session:
        settings = await session.get(AppSettings, 1)
    data = {
        **hidden(html, "/settings"),
        "quiet_start": "23:00",
        "quiet_end": "09:00",
        "digest_weekday": "0",
        "digest_time": "10:00",
        "default_notice_time": "12:30",
        "evening_time": "19:00",
        "later_minutes": "60",
        "payment_advance_days": "3",
        "balance_threshold_days": "5",
        "overdue_followup_hours": "24",
        "max_auto_messages_per_day": "4",
        "profanity_enabled": "1",
    }
    page = text_of(await web.follow(await web.post("/settings", data)))
    assert "Настройки сохранены; будущие напоминания пересчитаны. Сроки дел не менялись." in page
    async with x.app.sessions() as session:
        updated = await session.get(AppSettings, 1)
    assert (updated.later_minutes, updated.version) == (60, settings.version + 1)
    after = [j.scheduled_at for j in await x.pending_jobs(task_id)]
    assert after != before
    assert (await x.get(Task, task_id)).due_date == date(2026, 10, 12)

    fresh = hidden(await web.page("/settings"), "/settings")
    stale = {**data, "idem": fresh["idem"], "later_minutes": "90"}  # old version number
    stale = await web.post("/settings", stale)
    assert "обнови" in text_of(await web.follow(stale)).lower()


async def test_timezone_change_from_settings(web, x):
    html = await web.page("/settings")
    data = {**hidden(html, "/settings/timezone"), "timezone": "Europe/Moscow"}
    await web.follow(await web.post("/settings/timezone", data))
    async with x.app.sessions() as session:
        assert (await session.get(AppSettings, 1)).timezone == "Europe/Moscow"
    bad = {**hidden(await web.page("/settings"), "/settings/timezone"), "timezone": "Mars/Base"}
    page = text_of(await web.follow(await web.post("/settings/timezone", bad)))
    assert "Данные не менял" in page


async def test_history_lists_and_undoes_specific_action(web, x):
    task_id, _ = await new_task(web, title="Первое")
    await new_task(web, title="Второе")
    html = await web.page("/history")
    receipts = await x.all(CommandReceipt, CommandReceipt.command_type == "create_task")
    first = next(r for r in receipts if r.result["data"]["task_id"] == task_id)
    response = await web.post("/undo", hidden(html, "/undo", target=first.command_id))
    await web.follow(response)
    assert (await x.get(Task, task_id)).status != "open"
    titles = {t.title for t in await x.all(Task, Task.status == "open")}
    assert titles == {"Второе"}


async def test_reused_idem_with_other_content_is_refused(web, x):
    html = await web.page("/tasks/new")
    data = {**hidden(html, "/tasks"), "title": "Одно"}
    await web.post("/tasks", data)
    response = await web.post("/tasks", {**data, "title": "Другое"})
    assert response.status_code == 422
    assert "уже использован" in text_of(response.text)
    assert [t.title for t in await x.all(Task)] == ["Одно"]


async def test_not_found_and_foreign_ids(web):
    assert (await web.get("/tasks/" + "f" * 32)).status_code == 404
    assert (await web.get("/obligations/nope")).status_code == 404


async def test_health_endpoints_are_public_and_minimal(x):
    anonymous = Web(x)
    try:
        live = await anonymous.get("/health/live")
        assert live.json() == {"status": "ok"}
        ready = await anonymous.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ready",
            "checks": {"database": True, "schema": True, "background": "disabled"},
        }
        assert (await anonymous.get("/diagnostics")).status_code == 303  # private view
    finally:
        await anonymous.close()


async def test_diagnostics_view(web, x):
    page = text_of(await web.page("/diagnostics"))
    assert "Фоновый цикл не запущен в этом процессе" in page
    assert "Резервная копия не настроена (BACKUP_DIR)" in page
    assert "Модель отключена (degraded)" in page
    assert "Активные веб-сессии 1" in page


async def test_jobs_follow_web_changes(web, x):
    task_id, _ = await new_task(web, title="Позвонить", due_date="2026-10-10")
    assert await x.pending_jobs(task_id)
    card = await web.page(f"/tasks/{task_id}")
    await web.post(f"/tasks/{task_id}/complete", hidden(card, f"/tasks/{task_id}/complete"))
    assert await x.pending_jobs(task_id) == []
    async with x.app.sessions() as session:
        cancelled = await session.scalars(
            select(NotificationJob).where(
                NotificationJob.subject_id == task_id, NotificationJob.status == "cancelled"
            )
        )
        assert list(cancelled)


async def test_lifespan_starts_background_loop_once(x, tmp_path):
    from xboct.llm.fixture import FixtureIntentProvider
    from xboct.scheduling.lock import AlreadyRunning
    from xboct.scheduling.runtime import Runtime
    from xboct.telegram.transport import FakeTransport
    from xboct.web.app import WebOptions, create_app
    from xboct.web.auth import AuthService

    lock = tmp_path / "web.lock"

    def runtime() -> Runtime:
        return Runtime(
            x.app, FakeTransport(), FixtureIntentProvider(), 4242, lock_path=lock, poll_timeout=0
        )

    app = create_app(x.app, AuthService(x.app), WebOptions(secure_cookies=False, runtime=runtime()))
    async with app.router.lifespan_context(app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            ready = (await client.get("/health/ready")).json()
        assert ready["checks"]["background"] == "running"
        with pytest.raises(AlreadyRunning):
            await runtime().start()  # a second process/replica is refused
    second = runtime()
    await second.start()  # the lock was released on shutdown
    await second.stop()


@pytest.mark.parametrize(
    ("minor", "shown"),
    [
        (0, "0"),
        (1_000, "10"),
        (10_000, "100"),
        (100_000, "1000"),
        (120_000, "1200"),
        (120_050, "1200,5"),
        (7, "0,07"),
    ],
)
async def test_amount_round_trip_through_edit_form(web, x, minor, shown):
    result = await x.ok(c.CreateObligation(title="Подписка", kind="coverage", amount_minor=minor))
    ob_id = result.data["obligation_id"]
    form = await web.page(f"/obligations/{ob_id}/edit")
    assert f'name="amount" inputmode="decimal" value="{shown}"' in form
    # Changing only the note keeps the amount exactly as stored.
    data = {
        **hidden(form, f"/obligations/{ob_id}/edit"),
        "title": "Подписка",
        "amount": shown,
        "aliases": "",
        "note": "новая заметка",
    }
    page = text_of(await web.follow(await web.post(f"/obligations/{ob_id}/edit", data)))
    assert "Обновил «Подписка»." in page
    ob = await x.get(Obligation, ob_id)
    assert (ob.amount_minor, ob.note) == (minor, "новая заметка")
    # And the unchanged form submitted again is "nothing changed", not a silent rewrite.
    again = await web.page(f"/obligations/{ob_id}/edit")
    data = {
        **hidden(again, f"/obligations/{ob_id}/edit"),
        "title": "Подписка",
        "amount": shown,
        "aliases": "",
        "note": "новая заметка",
    }
    response = await web.post(f"/obligations/{ob_id}/edit", data)
    assert response.status_code == 422
    assert "Ничего не изменилось" in response.text
