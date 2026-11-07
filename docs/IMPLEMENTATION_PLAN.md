# Реализация по проверяемым этапам

Одна сессия — один этап и reviewable PR. Не строить сразу весь проект без промежуточной
проверки. Архитектор смотрит diff и результаты, затем выдаёт следующий этап.

## 1. Детерминированное ядро и хранение

Создать pyproject.toml, uv.lock, package, migrations; реализовать tasks/obligations,
recurrence/occurrences, payment/coverage/balance ledger, time resolver, receipts,
audit/undo и notification planning records. Typed application commands и fixture
IntentProvider, clock injection. Прямые интеграционные тесты на дисковой SQLite.
Тестовый сценарий complete -> restart -> следующие сроки/прогноз. Никаких ключей,
реального бота, web UI или внешних модельных запросов. Это самостоятельный полезный
этап, не только пустые классы/каркас. Добавить CI для pytest/Ruff без секретов.

## 2. Напоминания и Telegram

Persistent scheduler/outbox, polling inbox recovery, process lock, settings defaults,
owner checks, кнопки done/paid/snooze, недельная сводка и dedup. Fake transport по
умолчанию в тестах. Обработать API errors/unknown outcome. Реальный token пока не нужен.

## 3. Разговор через OpenAI API

AsyncOpenAI Responses adapter, strict propose_intent tool, schema normalization,
bounded context, ambiguity/confirmation, budget/call limits, failure fallback.
Mock HTTP tests реального SDK request shape, не только ручной fixture interface.
Создать eval corpus из согласованных фраз. Без live key этап выполняется полностью;
качество реальной модели остаётся отдельной будущей проверкой.

## 4. Веб-обзор и безопасное редактирование

Jinja2 + minimal JS; списки/карточки/формы/settings/history. Все mutations через общий
application layer; auth code via Telegram, sessions, CSRF. Desktop/mobile browser QA.
Без LLM все основные действия доступны. Не делать публичный deploy.

## 5. Пакет запуска и пилот

Dockerfile/Compose (одна реплика), env example, migration/start commands, persistent
volume, backup/restore, health diagnostics, release/rollback runbook. Cloud build +
контейнерный smoke если доступен Docker; отсутствие Docker не маскировать тестом Python.
Потом через Codex: выбрать VPS, domain/HTTPS, установить secrets и выполнить отдельно
разрешённый deploy/live tests. Не переносить production data в cloud-сессию.

## Review каждого PR

Проверить scope, tests, logs без секретов, совпадение с продуктом; объяснить пользователю
по-котячьи что появилось. Не merge автоматически. Новые product decisions фиксировать
в DECISIONS.md; неизвестные параметры не заменять молча новым стеком.
