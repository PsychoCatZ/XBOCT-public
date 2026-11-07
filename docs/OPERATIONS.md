# Запуск, безопасность и эксплуатация

Это требования к будущему пакету запуска, не уже существующие команды deploy.

## Production

Linux VPS выбирается после read-only проверки: OS/architecture, свободная память/диск,
существующие сервисы/порты, Docker availability, outbound HTTPS к Telegram/OpenAI,
backup destination. Ориентир для самого небольшого приложения — 1 vCPU/1 GB свободной
RAM и постоянный диск; это стартовый инженерный ориентир, не измеренный расход.
Близость к пользователю вторична доступности API/надёжности. Не трогать другие сервисы.

Один Docker Compose app service, one worker, restart policy unless-stopped; bind
application port на 127.0.0.1 за имеющимся reverse proxy либо добавить Caddy с HTTPS,
если proxy ещё нет. Domain выбирается до web live; SQLite только local persistent
volume /data. Secrets в закрытом env file на VPS, вне Git; container non-root.
Database migration выполняется один раз до старта worker; релиз привязан к commit/image.

## Конфигурация (реализована)

APP_ENV=test|demo|production; DATABASE_URL; USER_TIMEZONE;
TELEGRAM_BOT_TOKEN; TELEGRAM_OWNER_ID;
LLM_PROVIDER=fixture|openai; OPENAI_API_KEY; OPENAI_MODEL; OPENAI_REASONING_EFFORT;
LLM_DAILY_CALL_LIMIT; WEB_HOST; WEB_PORT; WEB_PUBLIC_ORIGIN (вместо PUBLIC_BASE_URL);
WEB_SESSION_DAYS (вместо AUTH_SESSION_TTL); WEB_SECURE_COOKIES; WEB_TRUSTED_PROXIES;
BACKUP_DIR; BACKUP_TIME. Образцы без secrets: `.env.example` (разработка) и
`deploy/xboct.env.example` (сервер). Production fail closed: нет token/owner, HTTPS origin,
Secure cookies или BACKUP_DIR — процесс не стартует; `--no-bot` и `--demo-login`
запрещены. LLM_PROVIDER=fixture — явное состояние degraded (видно в диагностике),
формы, кнопки и напоминания работают. Runbook: [DEPLOY.md](DEPLOY.md).

## Резервные копии

Ежедневный consistent snapshot через SQLite backup API, не простое копирование db
при активном WAL. Хранить 7 daily + 4 weekly, chmod owner-only; database + schema version
и release manifest. Тексты переписки/задач — приватные данные. До live-пилота настроить
копию вне основного VPS: локальная папка владельца или другой выбранный сервер.
Это обязательный эксплуатационный шаг, не нужна отдельная cloud backup платформа.

Restore в новый пустой путь; integrity_check + проверка ключевых сущностей и jobs.
Не запускать восстановленную копию с production Telegram token одновременно с оригиналом.
Перед migration backup; rollback приложения только если schema совместима. Если нужен
restore pre-migration snapshot, объяснить возможную потерю изменений после snapshot.

## Наблюдаемость

/health/live — процесс; /health/ready — БД/migrations/background loops. Readiness не
должна зависеть от LLM availability. В закрытом web diagnostic view: last scheduler
tick, last Telegram success/error, pending/unknown deliveries, last backup, LLM status.
Публичные health ответы без приватных данных. Structured logs: IDs, status, latency;
redact keys, auth codes, headers, full messages. Внешний uptime check до пилота: отсутствие
процесса нельзя надёжно сообщить через этот же умерший процесс.

## Live приёмка через Codex

После отдельного разрешения: build -> backup -> migration -> restart -> readiness ->
Telegram test -> phone/watch acknowledgement -> OpenAI intent test -> restart/persistence.
Тестовая задача/платёж не должны изменять настоящие обязательства. Проблема доставки
на часы разбирается по настройкам телефона, не объявляется исправленной серверным логом.

## Разделение ролей

Claude Cloud пишет и тестирует код, предлагает PR. Codex reviews, объясняет вопросы,
выбирает эксплуатационный план вместе с владельцем и выполняет разрешённые локальные/
серверные действия. Владелец делает вход в аккаунты/выбирает настройки и безопасно
предоставляет credentials, когда они нужны. Все запросы Claude к нему — через Codex.
