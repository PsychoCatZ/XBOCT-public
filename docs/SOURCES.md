# Первичные источники

Проверены 8 октября 2026. Технологические решения принадлежат архитектору; документация
подтверждает возможности инструментов, а не необходимость нашего конкретного стека.

- [Claude Code cloud environments](https://code.claude.com/docs/en/cloud-environments):
  repo configuration, сеть, setup и инструменты cloud.
- [Claude Code web quickstart](https://code.claude.com/docs/en/web-quickstart): вход и GitHub.
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling):
  strict tool proposals и контроль parallel calls.
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  schema constraints и отказ модели.
- [OpenAI Responses create](https://developers.openai.com/api/reference/python/resources/responses/methods/create):
  transport API. Доступ к конкретной модели аккаунта ещё не проверялся.
- [Telegram Bot API](https://core.telegram.org/bots/api): сообщения, callbacks, polling.
- [aiogram](https://docs.aiogram.dev/en/latest/): Python Telegram framework.
- [FastAPI templates](https://fastapi.tiangolo.com/advanced/templates/): Jinja2 UI.
- [SQLite appropriate uses](https://www.sqlite.org/whentouse.html): условия применимости.
- [SQLAlchemy SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html): storage adapter.
- [Python zoneinfo](https://docs.python.org/3.12/library/zoneinfo.html): IANA timezone.
- [uv locking/sync](https://docs.astral.sh/uv/concepts/projects/sync/): reproducible dependencies.

Model ID, доступность API с выбранного VPS и actual dependency versions проверяются
при реализации/подключении. Здесь нет неподтверждённых тарифов или обещаний доступа.
