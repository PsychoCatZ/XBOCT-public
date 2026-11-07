# Запуск на VPS, обновление, откат и восстановление

Runbook для Codex и владельца. Выбор VPS, домена, установка секретов и сам deploy —
отдельные шаги, которые выполняются только после явного разрешения (docs/OPERATIONS.md).
Здесь нет реальных адресов, токенов и ключей.

## Что разворачивается

- Один образ (`Dockerfile`), привязанный к commit: `XBOCT_RELEASE=<short sha>`.
- `compose.yaml`: одноразовый сервис `migrate` (снимок БД, если схема отстаёт, затем
  `alembic upgrade`) и сервис `app` (`xboct serve`: веб, Telegram polling, планировщик,
  отправка и ежедневный backup в одном процессе, один worker, process lock).
- Тома: `data` (`/data/xboct.db`, SQLite WAL) и `backups` (`/backups`).
- Порт приложения публикуется только на `127.0.0.1:8000` для HTTPS reverse proxy.
  Если proxy на сервере нет, профиль `caddy` поднимает Caddy с автоматическим HTTPS.
- Контейнер: пользователь `xboct` (uid 10001), read-only root, `no-new-privileges`,
  без capabilities; healthcheck — `/health/ready`.

## Сервер

Рабочий план (Codex): отдельный новый VPS с Ubuntu 24.04, HTTPS через Caddy из Compose
(профиль `caddy`), внешние копии ежедневно забираются на Moscow в приватный каталог.
Владелец купил VPS: 2 vCPU AMD EPYC, 4 GB RAM, 60 GB NVMe — с запасом против ориентира
(2 GB RAM, 25–30 GB SSD). Конкретный адрес, домен и доступы настраиваются через Codex.

Перед установкой (read-only): Ubuntu 24.04 x86_64, Docker Engine + Compose v2, свободные
порты 80/443, исходящий HTTPS к `api.telegram.org` (и к OpenAI, если модель включается),
DNS-запись домена на адрес VPS, SSH-доступ с Moscow для забора копий.

## Первый запуск

1. Код: `git clone` репозитория, `git checkout <commit>`.
2. Секреты: `sudo install -d -m 700 /etc/xboct` и файл `/etc/xboct/xboct.env` по образцу
   `deploy/xboct.env.example` (`chmod 600`, владелец root). Значения вводит владелец или
   Codex на сервере; они не попадают в Git, в переписку и в логи.
3. Сборка: `export XBOCT_RELEASE=$(git rev-parse --short HEAD)` и
   `docker compose build`.
4. Старт: `docker compose up -d` (с proxy на сервере) или
   `XBOCT_DOMAIN=<домен> docker compose --profile caddy up -d`.
   `migrate` создаёт схему и завершается, затем стартует `app`.
5. Проверки:
   - `docker compose ps` — `app` healthy, `migrate` exited 0;
   - `curl -s 127.0.0.1:8000/health/ready` → `"status":"ready"`, `"background":"running"`;
   - снаружи `https://<домен>/health/live` → `{"status":"ok"}`;
   - в Telegram владелец пишет боту `/today`, затем `/login` и входит в веб;
   - в вебе «Диагностика»: тик планировщика свежий, Telegram без ошибок, модель
     (или явное «degraded»), резервная копия появится после `BACKUP_TIME`;
   - `docker compose exec app xboct backup` — ручная копия, `integrity=ok`.
6. Кнопка Mini App (после установки релиза с Mini App): `docker compose exec app xboct
   telegram-setup --dry-run`, затем `... xboct telegram-setup`; вернуть обычное меню —
   `... xboct telegram-setup --reset`. Один вызов Bot API, только явно (docs/MINI_APP.md).
7. Внешний мониторинг `/health/live` (uptime-сервис по выбору владельца) — до пилота.

## Остановка

`docker compose stop app` (SIGTERM) завершает процесс корректно: ожидание прерывается,
сообщение в работе обрабатывается до конца и его ответ сохраняется в outbox, но новые
сообщения и новые отправки после сигнала не стартуют; уже начатая отправка заканчивается.
Ответы, не успевшие уйти, доставляются после следующего запуска (ничего не теряется и не
уходит дважды). В худшем случае остановка занимает до 70 с (модель 2 × 20 с, отправка 20 с,
блокировки БД 10 с — оценка с запасом); `stop_grace_period` — 90 с. Если менять таймауты
модели или Telegram, `test_stop_budget.py` потребует пересчитать период.

## Резервные копии

- Ежедневно после `BACKUP_TIME` (локальное время владельца) `app` делает snapshot через
  SQLite backup API, проверяет `integrity_check`, пишет manifest (schema, release, sha256,
  счётчики) и хранит 7 дневных + 4 недельных. Файлы 0600, каталог 0700.
- Ошибка копии видна в «Диагностике» и логах, повтор через час.
- Перед каждой миграцией `migrate` делает снимок `pre-migration` (в production без
  `BACKUP_DIR` миграция отказывается); перед релизом — ручной `pre-release`.
- Точки отката (`pre-migration`, `pre-release`) не участвуют в дневной/недельной
  ротации: хранятся последние три каждого типа.
- Копия вне VPS (обязательна до пилота): ежедневно забирать `/backups` на Moscow в
  приватный каталог (например, rsync по SSH из каталога тома
  `/var/lib/docker/volumes/xboct_backups/_data` от отдельного пользователя только на
  чтение; конкретная схема доступа — через Codex). Копии содержат личные данные.

## Обновление

Миграция никогда не идёт под работающим приложением: `xboct migrate` берёт тот же
эксклюзивный lock, что и `app`, и при работающем приложении отказывается
(«stop the app before migrating»), ничего не меняя.

1. `git fetch && git checkout <новый commit>`; прочитать изменения миграций в PR.
2. `docker compose exec app xboct backup --reason pre-release` — точка отката.
3. `export XBOCT_RELEASE=$(git rev-parse --short HEAD) && docker compose build`.
4. `docker compose stop app` — остановить старое приложение (напоминания на это время
   ждут в БД и уйдут после старта, как catch-up).
5. `docker compose up -d` — `migrate` (со снимком `pre-migration`, если схема
   отстаёт), затем новый `app`. Если `migrate` завершился ошибкой, `app` не стартует:
   разобраться по `docker compose logs migrate`, при необходимости откат (ниже).
6. Повторить проверки из «Первый запуск», п. 5.

## Откат

- Схема не менялась (`alembic` head тот же): `git checkout <старый commit>`, сборка
  с его `XBOCT_RELEASE`, `docker compose up -d`. Данные остаются.
- Схема изменилась: старый код не стартует на новой схеме (`SchemaNotReady`). Варианты:
  исправить вперёд новым релизом (предпочтительно) или восстановить снимок
  `pre-migration` (ниже) — тогда изменения после снимка теряются, это надо объяснить
  владельцу до действия.

## Восстановление

1. `docker compose stop app` — оригинал и восстановленная копия никогда не работают
   одновременно с production token.
2. Восстановить в новый путь и проверить:
   `docker compose run --rm --no-deps app xboct restore /backups/<файл>.db --to /data/restored.db`
   (integrity, sha256 из manifest, известная схема, счётчики таблиц).
3. Сохранить текущий файл: `mv /data/xboct.db /data/xboct.db.before-restore` (через
   `docker compose run --rm --no-deps app sh -c '...'`), удалить его `-wal`/`-shm`,
   затем `mv /data/restored.db /data/xboct.db`.
4. `docker compose up -d` — `migrate` поднимет схему, если копия старее, затем `app`.
5. Проверить дела, платежи и запланированные напоминания в вебе.

## Чего этот пакет не делает

Не выбирает VPS и домен, не устанавливает секреты, не выполняет deploy и live-проверки
Telegram/OpenAI — это отдельные шаги через Codex с разрешения владельца.
