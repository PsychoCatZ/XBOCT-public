# XBOCT

Персональный ассистент одного пользователя: дела, платежи, прогноз запаса средств,
напоминания в Telegram и краткая понедельничная сводка. Разговорный, дерзкий характер;
предсказуемая частота сообщений и честные формулировки о фактах и прогнозах.

## Состояние проекта

Реализованы задачи и повторения, три вида платежей (календарный платёж,
оплаченный период, баланс с оценкой расхода), напоминания в Telegram,
веб-интерфейс и Telegram Mini App. Создание обязательств через свободный текст
проходит через сохранённый предпросмотр и подтверждение.

Данные хранит SQLite; расчёты, подтверждения, журнал изменений и расписание
выполняет код. OpenAI используется для разбора свободного текста. Для разработки
есть fixture-provider и тесты без внешних сервисов. Проверки fixture не заменяют
live eval реальной модели.

Это очищенная публичная копия исходного проекта. В неё не входят история
приватной разработки, сведения о рабочем экземпляре, данные владельца и
внутренние отчёты. Примеры и регрессионные диалоги обезличены.
Инструкция самостоятельного развёртывания: [docs/DEPLOY.md](docs/DEPLOY.md).
Каждому экземпляру нужны собственные токен бота, Telegram ID владельца и ключ API.

Лицензия: [MIT](LICENSE). Имя репозитория — `XBOCT-public`;
имя Python-пакета и команды — `xboct`.

## Команды разработки

Поддерживаемая среда запуска и полного набора тестов — Linux (используется POSIX `fcntl`).
На Windows используй Linux-контейнер или WSL. Нужны Python 3.12 (production; 3.13 тоже проверяется в CI) и [uv](https://docs.astral.sh/uv/).
Все команды из корня репозитория; ключи, сеть к Telegram/OpenAI и SSH не нужны.

```bash
uv sync --frozen --group dev        # зависимости строго по uv.lock
uv run pytest                       # все тесты: дисковая SQLite, управляемые часы, fake LLM
uv run ruff check .                 # lint
uv run ruff format --check .        # формат
```

Для Linux доступен `bash scripts/cloud-bootstrap.sh`.

Миграции и офлайн-демо:

```bash
cp .env.example .env                # необязательно; значения по умолчанию те же
DATABASE_URL=sqlite+aiosqlite:///./data/xboct.db uv run xboct migrate
DATABASE_URL=sqlite+aiosqlite:///./data/xboct.db uv run alembic check   # схема = модели
uv run xboct demo --db /tmp/xboct-demo.db   # сценарий на новой БД с фиксированными часами
```

Запуск бота (одна реплика на файл БД; второй процесс откажется стартовать):

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/xboct.db uv run xboct migrate
TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_ID=... uv run xboct run
```

С `LLM_PROVIDER=openai`, `OPENAI_API_KEY` и `OPENAI_MODEL` свободный текст разбирает
OpenAI; без них — fixture. Лимит вызовов в день — `LLM_DAILY_CALL_LIMIT` (50).

Веб-интерфейс (бот, планировщик и отправка стартуют один раз из lifespan того же процесса):

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_ID=... uv run xboct serve   # вход: /login в боте; Mini App: /tma (docs/MINI_APP.md)
# локальное демо без бота: код входа печатается в терминал (только localhost, APP_ENV=demo)
WEB_SECURE_COOKIES=false uv run xboct serve --no-bot --demo-login
```

Браузерный QA на вымышленных данных (скриншоты и отчёт в `qa-artifacts/web/`):

```bash
uv sync --frozen --group dev --group qa
uv run python scripts/web_qa.py      # Chromium из /opt/pw-browsers или --chromium PATH
```

Резервные копии и контейнер:

```bash
BACKUP_DIR=$PWD/backups uv run xboct backup          # snapshot + проверка + retention
uv run xboct restore backups/xboct-<...>.db --to /tmp/restored.db   # только в новый путь
scripts/container_smoke.sh                           # образ + compose в demo-режиме
```

Production-запуск на VPS — по [docs/DEPLOY.md](docs/DEPLOY.md), оператором развёртывания.

Eval corpus (`evals/intent_corpus.json`):

```bash
uv run xboct eval                       # fixture: проверка корпуса и конвейера, бесплатно
uv run xboct eval --live --i-accept-paid-calls --out report.json   # реальная модель, платно
```

Live eval требует ключ и явное согласие на платные вызовы. Модель и reasoning effort
задаются явно: `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` (например, `none`). Отчёт
содержит запрошенную и вернувшуюся модель, effort, версию промпта, число API-попыток,
usage и время ответа по каждому случаю; неизвестный usage помечается как `unknown`.
Проверки — по смыслу: равнозначное название задачи допустимо, а сроки, суммы, цели,
статусы и отсутствие лишних изменений проверяются строго. Его выполняет Codex
отдельно: результаты fixture-режима не говорят о качестве модели.

`xboct run` запускает Telegram-бота без веб-интерфейса. Без `TELEGRAM_*` команда отказывается стартовать и не печатает
значения. Без модели свободный текст честно получает «не разобрал», а команды
`/today`, `/payments`, `/undo`, кнопки и напоминания работают.

`xboct demo` пишет только в новый файл (существующий не трогает) и печатает ответы,
повтор той же команды (`replayed=True`), список дел/платежей и запланированные записи
уведомлений. Ничего не отправляет и в production не запускается. Конфигурация — в
[.env.example](.env.example) и [deploy/xboct.env.example](deploy/xboct.env.example), без секретов.

## Как устроен код

```text
src/xboct/
  domain/       время и DST, выражения дат, повторы, coverage, прогноз баланса, деньги
  storage/      SQLAlchemy-модели, async engine (WAL, foreign keys, BEGIN IMMEDIATE), Alembic,
                snapshot/retention/restore
  scheduling/   runtime (тик, polling, refresh), диспетчер напоминаний, outbox, сводка, lock
  telegram/     transport (fake + aiogram), inbox, маршрутизация обновлений, токены кнопок
  application/  команды, обработчики, транзакция с receipt/audit, планировщик уведомлений,
                undo, pending intents, read models, русские шаблоны ответов, intent pipeline
  llm/          контракт, strict-схема, OpenAI Responses adapter, fixture, eval runner
  web/          FastAPI-приложение, вход по коду и по initData (Mini App), формы -> команды,
                read models, шаблоны
  cli.py        xboct migrate / demo / run / serve / backup / restore / eval / telegram-setup
alembic/        миграции (0001 — схема, 0002 — scheduler/outbox/inbox, 0003 — журнал LLM, 0004 — веб-вход)
evals/          корпус согласованных фраз для eval
tests/          domain/ — чистые правила; integration/ — Application на файле SQLite
```

Одна мутация = одна короткая транзакция: изменение, audit, пересчитанные записи
уведомлений и receipt команды. Повтор того же `command_id` возвращает сохранённый
результат и ничего не применяет повторно. Модель только предлагает: ссылки проверяются по
переданным кандидатам, даты/суммы/периоды вычисляет код, версии берутся из снимка
кандидатов. Текст успеха строится по сохранённому состоянию.

## Покрытие приёмки

| ID | Где проверено |
|---|---|
| A01–A05, «следующая неделя», preview | `tests/integration/test_intents.py`, `test_tasks.py` (через fixture-предложения) |
| A06–A08 | `tests/integration/test_payments.py`, `tests/domain/test_recurrence.py` |
| A09 | `test_intents.py::test_a09_until_january_needs_exact_boundary` |
| A10–A13, A24 | `test_payments.py`, `tests/domain/test_balance.py` |
| A14, A15 | `test_intents.py` (preview, атомарное подтверждение, устаревший preview, уточнение) |
| A16–A17 | `test_idempotency.py`: application idempotency/receipts; Telegram inbox/ack — этап 2 |
| A18 | `test_idempotency.py`, `test_scenario.py`: состояние после рестарта (полноценно) |
| A25 | `test_undo.py` |
| A29 | `test_migrations.py`; `test_backup.py`: snapshot при открытом приложении, 0600, manifest, retention, restore в новый путь (jobs/receipts/history), подмена файла, неизвестная схема, снимок перед миграцией, ежедневный backup в Runtime; `scripts/container_smoke.sh` |
| A31 | `test_timezone.py`, `tests/domain/test_time.py` |
| План этапа 1 | `test_scenario.py`: выполнить → рестарт → следующие сроки/прогноз |
| A16–A17 (Telegram) | `test_telegram_inbox.py`: повтор update, сбой между commit и ack, рестарт с непрочитанным inbox; `test_delivery.py`: двойное нажатие |
| A19 | `test_telegram_inbox.py`, `test_delivery.py`: модель недоступна — честный ответ, кнопки и напоминания работают |
| A20 | `test_digest_runtime.py`, `test_delivery.py`: тихие часы, рестарт во вторник, одна catch-up/сводка |
| A21 | `test_digest_runtime.py::test_monday_digest_once_even_with_retry_and_restart` |
| A22 | `test_digest_runtime.py`: подтверждённая сводка подавляет дубль, точный reminder остаётся; неотправленная не подавляет |
| A23 | `test_delivery.py`: unknown → повтор не раньше 30 мин, максимум 2 попытки; сбой посреди отправки |
| A26 | `test_telegram_inbox.py`: чужой пользователь, группа, поддельные кнопки |
| A30 | `test_digest_runtime.py::test_second_instance_refuses_to_start` |
| A32 | `test_digest_runtime.py`: «напоминание отправлялось» только по доставкам, оплата — по платежам |
| Этап 3: контракт API | `test_openai_contract.py`: настоящий SDK против mock HTTP — форма запроса, strict-схема, refusal/incomplete/invalid, один повтор, лимит вызовов, журнал |
| Этап 3: конкурентность | `test_runtime_llm.py`: медленная модель не останавливает напоминания; версии перепроверяются после ответа; «да» — только актуальное preview |
| Этап 3: eval corpus | `test_evals.py`: идеальные предложения через реальный конвейер (не оценка модели) |
| A27 | `test_web_auth.py`: одноразовый код, срок кода и сессии, 5 попыток, троттлинг, CSRF/Origin (и для логина), отзыв сессий, логи без кода/токена/IP, редакция кода в outbox |
| Ответы о платежах (после запуска) | `test_payment_query_filters.py`, `test_current_payment_status.py`, `test_payment_overdue_block.py` (просроченное отдельным блоком, месяц без года) |
| «Оплачено сейчас» для календарных платежей | `test_calendar_paid_semantics.py`: квартальный и годовой платёж не превращаются в покрытие |
| Пределы остановки | `test_stop_budget.py`: grace period Compose покрывает худший случай, попытка модели ограничена целиком, после остановки не стартуют новые сообщения |
| Диагностика eval и согласованность prompt со схемой | `test_eval_diagnostics.py`: воспроизведение `agenda`+период через SDK/mock HTTP, ошибки валидации и вопрос уточнения в отчёте без секретов, prompt упоминает все значения схемы |
| Остановка Runtime | `test_runtime_stop.py`: остановка во время записи в БД, BEGIN в ожидании блокировки, закрытие сессии; блокировка процесса до конца фоновых задач |
| A33 | `test_balance_final.py`: запас 4 дня при пороге 5, финальное предупреждение, пересчёт пополнением, ≤ 1 суток, тихие часы, одна отправка |
| A28 | `test_web_flows.py::test_a28_notes_are_escaped_and_never_commands`, `javascript:`-ссылки отклоняются; строгий CSP |
| Этап 4: формы | `test_web_flows.py`: дела и три вида платежей через отрендеренные формы, двойная отправка, конфликт версии, отмена, настройки и пересчёт напоминаний, health, lifespan с одним Runtime |

Естественные фразы в тестах идут через fixture-провайдер: это проверка контракта, а не
оценка качества модели.

## Документация

1. [Продукт и границы MVP](docs/PRODUCT.md).
2. [Архитектура и технологии](docs/ARCHITECTURE.md).
3. [Модель данных и инварианты](docs/DATA_MODEL.md).
4. [Даты и расчёты](docs/TIME_AND_PAYMENTS.md).
5. [LLM и команды](docs/LLM_CONTRACT.md).
6. [Уведомления](docs/NOTIFICATIONS.md).
7. [Приёмка](docs/ACCEPTANCE.md).
8. [Этапы реализации](docs/IMPLEMENTATION_PLAN.md).
9. [Эксплуатация и доступ](docs/OPERATIONS.md).
10. [Решения, допущения и открытые вопросы](docs/DECISIONS.md).
11. [Проверенные первичные источники](docs/SOURCES.md).
