"""Command line: migrate, offline demo, bot, web, eval.

The demo uses a fixed clock and the fixture provider: no network, no keys, no bot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from xboct.application import commands as c
from xboct.application.context import CommandContext
from xboct.application.intents import IntentService
from xboct.application.queries import agenda, agenda_text, payments, payments_text
from xboct.application.render import fmt_instant
from xboct.application.service import Application
from xboct.application.uow import Uow
from xboct.config import ConfigError, load_config
from xboct.domain.clock import FixedClock
from xboct.domain.timeutil import get_zone
from xboct.llm.fixture import FixtureIntentProvider
from xboct.storage import migrate
from xboct.storage.backup import BackupError
from xboct.storage.db import sqlite_url


def cmd_migrate() -> int:
    """Upgrade to head. An existing database behind head is snapshotted first when
    BACKUP_DIR is set (required in production). Refuses while the app holds the lock."""
    from xboct.config import load_backup_config
    from xboct.scheduling.lock import AlreadyRunning, ProcessLock
    from xboct.scheduling.runtime import lock_path_for

    config = load_config()
    backup = load_backup_config()
    # The same exclusive lock as the running app: never migrate under a live runtime.
    lock = ProcessLock(lock_path_for(config.database_url))
    try:
        lock.acquire()
    except AlreadyRunning:
        print(
            "XBOCT is running on this database: stop the app before migrating "
            "(docker compose stop app)",
            file=sys.stderr,
        )
        return 2
    try:
        return _migrate_locked(config, backup)
    finally:
        lock.release()


def _migrate_locked(config: Any, backup: Any) -> int:
    from xboct.storage.backup import create_snapshot
    from xboct.storage.db import database_path

    current = migrate.current_revision(config.database_url)
    if current is not None and current != migrate.head_revision():
        if backup.directory is None:
            if config.app_env == "production":
                print("production migration needs BACKUP_DIR for a snapshot", file=sys.stderr)
                return 2
        else:
            db_file = database_path(config.database_url)
            assert db_file is not None
            snapshot = create_snapshot(
                db_file, backup.directory, now=datetime.now(UTC), reason="pre-migration"
            )
            print(f"pre-migration backup {snapshot.path}")
    migrate.upgrade(config.database_url)
    print(f"schema at {migrate.current_revision(config.database_url)}")
    return 0


async def _demo(path: Path) -> None:
    url = sqlite_url(path)
    migrate.upgrade(url)
    tz = get_zone("Asia/Krasnoyarsk")
    clock = FixedClock(datetime(2026, 10, 8, 10, 0, tzinfo=tz))
    provider = FixtureIntentProvider(
        {
            "ответить Юле в течение недели": {
                "kind": "actions",
                "actions": [
                    {
                        "type": "create_task",
                        "title": "Ответить Юле",
                        "due": {"date": {"kind": "within_week"}},
                    }
                ],
            }
        }
    )
    app = await Application.open(url, clock)
    intents = IntentService(app, provider)
    n = 0

    def ctx() -> CommandContext:
        nonlocal n
        n += 1
        return CommandContext(command_id=f"demo:{n}", source="cli", received_at=clock.now())

    def show(text: str) -> None:
        print(f"> {text}\n")

    try:
        show((await intents.handle_message("ответить Юле в течение недели", ctx())).message)
        vps = await app.execute(
            c.CreateObligation(
                title="VPS",
                kind="calendar",
                recurrence=c.RecurrenceSpec(frequency="monthly", anchor_date=date(2026, 10, 15)),
            ),
            ctx(),
        )
        show(vps.message)
        hetzner = await app.execute(
            c.CreateObligation(
                title="Hetzner",
                kind="balance",
                balance=c.BalanceStart(assume_zero=True, rate_minor_per_day=4_000),
            ),
            ctx(),
        )
        topup = c.RecordTopup(obligation_id=hetzner.data["obligation_id"], amount_minor=120_000)
        show((await app.execute(topup, ctx())).message)
        repeat = await app.execute(topup, CommandContext("demo:3", "cli", clock.now()))
        show(f"(повтор той же команды: replayed={repeat.replayed})")
        show(await app.read(_agenda))
        show(await app.read(_payments))
        print("Запланированные уведомления (только записи; отправка — этап 2):")
        for job in await app.jobs(status="pending"):
            print(f"  {job.kind:<12} {fmt_instant(job.scheduled_at, tz)}  {job.dedup_key}")
    finally:
        await app.close()


async def _agenda(uow: Uow) -> str:
    return agenda_text(uow, await agenda(uow))


async def _payments(uow: Uow) -> str:
    return payments_text(uow, await payments(uow))


def cmd_demo(db: str) -> int:
    config = load_config()
    if config.app_env == "production":
        print("demo is not allowed in production", file=sys.stderr)
        return 2
    path = Path(db)
    if path.exists():
        print(f"{path} exists; the demo only writes a fresh database", file=sys.stderr)
        return 2
    asyncio.run(_demo(path))
    print(f"\nDemo database: {path}")
    return 0


def _provider(llm: Any) -> Any:
    from xboct.llm.openai_provider import OpenAIIntentProvider

    if llm.provider == "openai":
        return OpenAIIntentProvider(
            model=llm.model or "", api_key=llm.api_key, reasoning_effort=llm.reasoning_effort
        )
    return FixtureIntentProvider()  # degraded: free text gets an honest "не разобрал"


def _logging() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def cmd_run() -> int:
    """Bot, scheduler and sender without the web (`serve` runs both in one process)."""
    from xboct.config import (
        check_production,
        load_backup_config,
        load_bot_config,
        load_llm_config,
        load_web_config,
    )
    from xboct.domain.clock import SystemClock
    from xboct.scheduling.runtime import Runtime
    from xboct.telegram.aiogram_transport import AiogramTransport
    from xboct.web.auth import AuthService

    config = load_config()
    backup = load_backup_config()
    check_production(config, load_web_config(), backup)
    bot_config = load_bot_config()
    llm = load_llm_config()
    _logging()

    async def run() -> None:
        app = await Application.open(
            config.database_url, SystemClock(), default_timezone=config.user_timezone
        )
        transport = AiogramTransport(bot_config.token)
        runtime = Runtime(
            app,
            transport,
            _provider(llm),
            bot_config.owner_id,
            daily_call_limit=llm.daily_call_limit,
            auth=AuthService(app),
            backup_dir=backup.directory,
            backup_time=backup.daily_at,
        )
        try:
            await runtime.run()
        finally:
            await transport.close()
            await app.close()

    asyncio.run(run())
    return 0


def cmd_serve(*, no_bot: bool, demo_login: bool) -> int:
    """Web interface; the bot, scheduler, sender and daily backup start once from the
    ASGI lifespan (one process, one worker, process lock).

    --no-bot runs only the web (no reminders are sent from this process). --demo-login
    prints a one-time login code to this terminal; both are refused in production, and
    --demo-login also outside localhost. The code never goes to logs.
    """
    import uvicorn

    from xboct.config import (
        LOCAL_HOSTS,
        check_production,
        load_backup_config,
        load_bot_config,
        load_llm_config,
        load_web_config,
    )
    from xboct.domain.clock import SystemClock
    from xboct.scheduling.runtime import lock_path_for
    from xboct.web.app import WebOptions, create_app
    from xboct.web.auth import AuthService
    from xboct.web.tma import TmaConfig

    config = load_config()
    web = load_web_config()
    backup = load_backup_config()
    llm = load_llm_config()
    check_production(config, web, backup)
    production = config.app_env == "production"
    if demo_login and (production or web.host not in LOCAL_HOSTS):
        print("--demo-login needs APP_ENV=test/demo and a localhost WEB_HOST", file=sys.stderr)
        return 2
    if no_bot and production:
        print("--no-bot is not allowed in production: reminders would stop", file=sys.stderr)
        return 2
    bot_config = None if no_bot else load_bot_config()
    _logging()

    async def build() -> tuple[Application, AuthService]:
        app = await Application.open(
            config.database_url, SystemClock(), default_timezone=config.user_timezone
        )
        return app, AuthService(app, session_ttl=timedelta(days=web.session_days))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    core, auth = loop.run_until_complete(build())
    runtime = None
    if bot_config is not None:
        from xboct.scheduling.runtime import Runtime
        from xboct.telegram.aiogram_transport import AiogramTransport

        runtime = Runtime(
            core,
            AiogramTransport(bot_config.token),
            _provider(llm),
            bot_config.owner_id,
            daily_call_limit=llm.daily_call_limit,
            auth=auth,
            backup_dir=backup.directory,
            backup_time=backup.daily_at,
        )
    if demo_login:
        code = loop.run_until_complete(auth.issue_code())
        print(f"demo login code (5 minutes, one use): {code}", flush=True)
    app = create_app(
        core,
        auth,
        WebOptions(
            secure_cookies=web.secure_cookies,
            public_origin=web.public_origin,
            runtime=runtime,
            llm={"provider": llm.provider, "model": llm.model, "limit": llm.daily_call_limit},
            backup_dir=backup.directory,
            lock_path=lock_path_for(config.database_url),
            close_app_on_shutdown=True,
            tma=TmaConfig(bot_config.token, bot_config.owner_id) if bot_config else None,
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=web.host,
            port=web.port,
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips=web.trusted_proxies,
            log_level="info",
        )
    )
    loop.run_until_complete(server.serve())
    return 0


def cmd_telegram_setup(*, reset: bool, dry_run: bool) -> int:
    """Owner chat menu button for the Mini App (explicit; one Bot API call, no polling)."""
    from xboct.config import load_bot_config, load_web_config
    from xboct.telegram.menu import (
        BUTTON_TEXT,
        AiogramMenuClient,
        MenuError,
        apply_menu,
        mini_app_url,
    )

    bot_config = load_bot_config()
    web = load_web_config()
    try:
        if dry_run:
            what = (
                "the ordinary commands menu"
                if reset
                else f"«{BUTTON_TEXT}» -> {mini_app_url(web.public_origin)}"
            )
            print(f"dry run, nothing sent: chat {bot_config.owner_id} would get {what}")
            return 0

        async def run() -> str:
            client = AiogramMenuClient(bot_config.token)
            try:
                return await apply_menu(client, bot_config.owner_id, web.public_origin, reset=reset)
            finally:
                await client.close()

        print(asyncio.run(run()))
        return 0
    except MenuError as exc:
        print(f"telegram-setup: {exc}", file=sys.stderr)
        return 1


def cmd_backup(reason: str) -> int:
    """Consistent snapshot of DATABASE_URL into BACKUP_DIR (+ retention)."""
    from xboct.config import load_backup_config
    from xboct.storage.backup import create_snapshot, prune
    from xboct.storage.db import database_path

    config = load_config()
    backup = load_backup_config()
    if backup.directory is None:
        print("BACKUP_DIR is not set", file=sys.stderr)
        return 2
    db_file = database_path(config.database_url)
    assert db_file is not None
    snapshot = create_snapshot(db_file, backup.directory, now=datetime.now(UTC), reason=reason)
    removed = prune(backup.directory, tz=get_zone(config.user_timezone))
    m = snapshot.manifest
    print(
        f"backup {snapshot.path} schema={m['schema_revision']} integrity={m['integrity']} "
        f"counts={m['counts']} pruned={len(removed)}"
    )
    return 0


def cmd_restore(snapshot: str, target: str) -> int:
    """Restore a snapshot into a NEW file and verify it. Starts nothing."""
    from xboct.storage.backup import restore

    info = restore(Path(snapshot), Path(target), known_revisions=migrate.known_revisions())
    print(
        f"restored {target}: integrity={info['integrity']} schema={info['schema_revision']} "
        f"counts={info['counts']} (from {info['manifest']['created_at']}, "
        f"release {info['manifest']['release']})"
    )
    if info["schema_revision"] != migrate.head_revision():
        print("schema is older than this release: run `xboct migrate` on the restored copy")
    print("Do not run it with the production bot token while the original is running.")
    return 0


def _diagnostic_lines(diagnostics: dict) -> list[str]:
    """Short, structural lines for a failed eval case (no keys, no raw model output)."""
    lines = []
    proposal = diagnostics.get("proposal")
    if proposal:
        lines.append(f"proposal: {json.dumps(proposal, ensure_ascii=False)}")
    for issue in diagnostics.get("validation_errors", []):
        lines.append(f"validation: {issue['where']}: {issue['message']} ({issue['type']})")
    error = diagnostics.get("provider_error")
    if error:
        lines.append(f"provider error: {error['kind']} {error['detail']}".rstrip())
    message = diagnostics.get("result", {}).get("message")
    if message:
        lines.append(f"application said: {message}")
    return lines


def cmd_eval(*, live: bool, paid_ok: bool, out: str | None, only: list[str] | None = None) -> int:
    """Intent corpus. Default: fixture mode (contract check, free). --live: real model."""
    from xboct.config import load_llm_config
    from xboct.llm.evals import load_corpus, run_corpus
    from xboct.llm.openai_provider import PROMPT_VERSION, OpenAIIntentProvider

    try:
        cases = load_corpus(only=only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    provider = None
    model = None
    effort = None
    if live:
        llm = load_llm_config()
        if llm.provider != "openai":
            print("--live needs LLM_PROVIDER=openai, OPENAI_API_KEY, OPENAI_MODEL", file=sys.stderr)
            return 2
        if not paid_ok:
            print(
                f"--live makes about {len(cases)}-{2 * len(cases)} paid API calls; "
                "add --i-accept-paid-calls to run it",
                file=sys.stderr,
            )
            return 2
        provider = OpenAIIntentProvider(
            model=llm.model or "", api_key=llm.api_key, reasoning_effort=llm.reasoning_effort
        )
        model = llm.model
        effort = llm.reasoning_effort
    report = asyncio.run(
        run_corpus(
            cases,
            provider=provider,
            model=model,
            reasoning_effort=effort,
            prompt_version=PROMPT_VERSION if live else None,
        )
    )
    text = report.to_json()
    if out:
        Path(out).write_text(text, encoding="utf-8")
    for result in report.results:
        mark = "ok  " if result.passed else "FAIL"
        call = result.call
        print(
            f"{mark} {result.id}: {result.status} [attempts={call.attempts} "
            f"in={call.input_tokens} out={call.output_tokens} ms={call.latency_ms} "
            f"model={call.returned_model}]"
            + ("" if result.passed else f" — {'; '.join(result.failures)}")
        )
        if not result.passed:
            for line in _diagnostic_lines(result.diagnostics):
                print(f"       {line}")
    totals = report.totals()
    print(
        f"{report.mode}: {report.passed}/{len(report.results)} passed; requested model "
        f"{report.requested_model or '-'}, effort {report.reasoning_effort or 'not sent'}, "
        f"prompt {report.prompt_version or '-'}; attempts {totals['api_attempts']}, "
        f"tokens in {totals['input_tokens']} / out {totals['output_tokens']}"
    )
    return 0 if report.passed == len(report.results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xboct")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply Alembic migrations to DATABASE_URL")
    demo = sub.add_parser("demo", help="offline scenario on a fresh SQLite file")
    demo.add_argument("--db", required=True, help="path for a NEW database file")
    sub.add_parser("run", help="run the Telegram bot, scheduler and sender (one instance)")
    backup = sub.add_parser("backup", help="consistent snapshot of the database into BACKUP_DIR")
    backup.add_argument("--reason", default="manual", choices=["manual", "pre-release"])
    restore = sub.add_parser("restore", help="restore a snapshot into a NEW database file")
    restore.add_argument("snapshot", help="path to xboct-*.db from BACKUP_DIR")
    restore.add_argument("--to", required=True, help="path of a database file that must not exist")
    serve = sub.add_parser("serve", help="web interface (+ bot and scheduler in the lifespan)")
    serve.add_argument("--no-bot", action="store_true", help="web only, no Telegram/scheduler")
    serve.add_argument("--demo-login", action="store_true", help="print a login code (local demo)")
    setup = sub.add_parser(
        "telegram-setup", help="owner chat menu button «Открыть приложение» (explicit call)"
    )
    setup.add_argument("--reset", action="store_true", help="restore the ordinary commands menu")
    setup.add_argument("--dry-run", action="store_true", help="show the change, send nothing")
    evals = sub.add_parser("eval", help="intent corpus: fixture contract check or live model")
    evals.add_argument("--live", action="store_true", help="call the configured real model")
    evals.add_argument("--i-accept-paid-calls", dest="paid_ok", action="store_true")
    evals.add_argument("--out", help="write the JSON report here")
    evals.add_argument("--only", help="comma-separated case ids (a subset of the corpus)")
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            return cmd_migrate()
        if args.command == "run":
            return cmd_run()
        if args.command == "backup":
            return cmd_backup(args.reason)
        if args.command == "restore":
            return cmd_restore(args.snapshot, args.to)
        if args.command == "serve":
            return cmd_serve(no_bot=args.no_bot, demo_login=args.demo_login)
        if args.command == "telegram-setup":
            return cmd_telegram_setup(reset=args.reset, dry_run=args.dry_run)
        if args.command == "eval":
            return cmd_eval(
                live=args.live,
                paid_ok=args.paid_ok,
                out=args.out,
                only=args.only.split(",") if args.only else None,
            )
        return cmd_demo(args.db)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
