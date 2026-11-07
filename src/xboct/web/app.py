"""Web interface: lists, cards, forms, settings, history, diagnostics.

Server-rendered (Jinja2, autoescape) with a few lines of optional JS. Every mutation is
a typed application command executed through `Application.execute` with command_id
`web:<idem>`; the idem key is rendered into each form, so a double submit or a browser
retry replays the stored receipt instead of applying anything twice. Forms carry the
versions the owner saw; a stale form gets a version conflict, not a silent overwrite.

Security (docs/ARCHITECTURE.md «Веб-доступ»): POST only for changes; every POST checks
the request origin and a CSRF token (session HMAC; double-submit cookie for login);
session cookie HttpOnly + SameSite=Lax (+ Secure outside localhost); strict CSP without
inline scripts; nothing secret goes to logs or URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.datastructures import FormData

from xboct.application import commands as c
from xboct.application.context import CommandContext, CommandResult
from xboct.application.render import fmt_date, fmt_money, fmt_period
from xboct.application.service import Application
from xboct.application.uow import Uow
from xboct.domain.coverage import paid_through
from xboct.domain.money import minor_to_input
from xboct.domain.timeutil import get_zone
from xboct.scheduling.lock import ProcessLock
from xboct.storage import migrate
from xboct.storage.models import CommandReceipt, Obligation, Occurrence, Recurrence, Task
from xboct.web import forms, views
from xboct.web.auth import AuthService, LoginThrottled, SessionInfo, same_token
from xboct.web.tma import MAX_BODY_BYTES, InitDataError, TmaConfig, validate_init_data

log = logging.getLogger("xboct.web")

HERE = Path(__file__).parent
SESSION_COOKIE = "xboct_session"
LOGIN_COOKIE = "xboct_login"
TMA_COOKIE = "__Host-xboct_tma"  # Mini App session; needs Secure, so plain name on localhost
TMA_HINT_COOKIE = "__Host-xboct_tma_ctx"  # "this browser context is the Mini App"; not a credential
TMA_HINT_DAYS = 30
TELEGRAM_FRAME_ANCESTORS = (
    "https://web.telegram.org",
    "https://webk.telegram.org",
    "https://webz.telegram.org",
)
TELEGRAM_SDK = "https://telegram.org/js/telegram-web-app.js"
WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def build_csp(*, script_src: str = "'self'", frame_ancestors: str = "'none'") -> str:
    return (
        f"default-src 'self'; script-src {script_src}; style-src 'self'; img-src 'self' data:; "
        f"form-action 'self'; frame-ancestors {frame_ancestors}; base-uri 'none'; object-src 'none'"
    )


CSP = build_csp()  # the ordinary web: nothing external, never framed
_TELEGRAM_ANCESTORS = " ".join(TELEGRAM_FRAME_ANCESTORS)
CSP_TMA_FRAME = build_csp(frame_ancestors=_TELEGRAM_ANCESTORS)  # framed by Telegram Web only
CSP_TMA = build_csp(script_src=f"'self' {TELEGRAM_SDK}", frame_ancestors=_TELEGRAM_ANCESTORS)


@dataclass
class WebOptions:
    secure_cookies: bool = True
    public_origin: str | None = None
    runtime: Any = None  # scheduling.runtime.Runtime, started in lifespan when given
    llm: dict[str, Any] = field(default_factory=lambda: {"provider": "fixture"})
    backup_dir: Path | None = None
    lock_path: Path | None = None  # held by a web-only process (no runtime)
    close_app_on_shutdown: bool = False
    tma: TmaConfig | None = None  # Mini App sign-in; /tma is disabled (404) without it


class NotLoggedIn(Exception):
    pass


class Forbidden(Exception):
    pass


class NotFoundPage(Exception):
    pass


def safe_path(value: Any, default: str) -> str:
    """Only same-site absolute paths: no scheme, no host, no protocol-relative tricks."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ch in value for ch in "\r\n\t") or urlsplit(value).netloc:
        return default
    return value


def with_done(url: str, command_id: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}done={quote(command_id, safe='')}"


def _tma_cookie_header(
    name: str, value: str, max_age: int, *, secure: bool, httponly: bool = True
) -> str:
    """Set-Cookie text. Secure origins: Secure; SameSite=None; Partitioned (the cookie must
    survive inside Telegram Web's iframe). Plain localhost: Lax, for local development."""
    parts = [f"{name}={value}", f"Max-Age={max_age}", "Path=/"]
    if httponly:
        parts.append("HttpOnly")
    if secure:
        parts += ["Secure", "SameSite=None", "Partitioned"]
    else:
        parts.append("SameSite=Lax")
    return "; ".join(parts)


def remember_tma_cookie(response: Response, name: str, token: str, ttl_seconds: int) -> None:
    response.headers.append(
        "set-cookie",
        _tma_cookie_header(name, token, ttl_seconds, secure=name.startswith("__Host-")),
    )


def forget_tma_cookie(response: Response, name: str) -> None:
    """Same attributes as when it was set (a partitioned cookie is deleted only so)."""
    response.headers.append(
        "set-cookie", _tma_cookie_header(name, "", 0, secure=name.startswith("__Host-"))
    )


def remember_tma_hint(response: Response, name: str) -> None:
    response.headers.append(
        "set-cookie",
        _tma_cookie_header(name, "1", TMA_HINT_DAYS * 86400, secure=name.startswith("__Host-")),
    )


def forget_tma_hint(response: Response, name: str) -> None:
    forget_tma_cookie(response, name)


def create_app(core: Application, auth: AuthService, options: WebOptions | None = None) -> FastAPI:
    opts = options or WebOptions()
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        fmt_money=fmt_money,
        fmt_period=fmt_period,
        weekdays_full=WEEKDAYS_FULL,
        setting_labels=forms.SETTING_LABELS,
    )
    head = migrate.head_revision()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = opts.runtime
        task = None
        web_lock = None
        if runtime is not None:
            await runtime.start()  # refuses a second instance (process lock)
            task = asyncio.create_task(runtime.loop())
        elif opts.lock_path is not None:
            # Web-only process: still the one app on this database, so migrations and a
            # second instance are refused while it runs.
            web_lock = ProcessLock(opts.lock_path)
            web_lock.acquire()
        app.state.runtime_task = task
        try:
            yield
        finally:
            if runtime is not None and task is not None:
                runtime.request_stop()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if web_lock is not None:
                web_lock.release()
            if opts.close_app_on_shutdown:
                await core.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime_task = None
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # --- plumbing ----------------------------------------------------------------------

    tma_cookie = TMA_COOKIE if opts.secure_cookies else "xboct_tma"
    tma_hint = TMA_HINT_COOKIE if opts.secure_cookies else "xboct_tma_ctx"

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        path = request.url.path
        entry = path == "/tma" or path.startswith("/tma/")
        in_app = bool(getattr(request.state, "tma", False))
        hinted = request.cookies.get(tma_hint) == "1"
        # Mini App responses may be framed by Telegram Web (exact origins, no wildcard) and,
        # on pages that load the SDK, may run it from telegram.org. The ordinary web keeps
        # `frame-ancestors 'none'` and no external script.
        if entry or in_app:
            response.headers["Content-Security-Policy"] = CSP_TMA
        elif hinted:
            response.headers["Content-Security-Policy"] = CSP_TMA_FRAME
        else:
            response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if not (entry or in_app or hinted):
            response.headers["X-Frame-Options"] = "DENY"
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def expected_origin(request: Request) -> str:
        if opts.public_origin:
            return opts.public_origin
        return f"{request.url.scheme}://{request.headers.get('host', '')}"

    def origin_ok(request: Request) -> bool:
        expected = expected_origin(request)
        origin = request.headers.get("origin")
        if origin:
            return origin == expected
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            return f"{parts.scheme}://{parts.netloc}" == expected
        return False  # a browser form POST always sends one of them

    async def current_session(request: Request) -> SessionInfo | None:
        """The one session of this request; its token alone decides authorisation, CSRF and
        logout, so cookies are never mixed.

        In a Mini App context (the marker cookie says so) only the Mini App session counts:
        after a failed or foreign Telegram sign-in there is no quiet fallback to an ordinary
        web session. The marker grants nothing; the ordinary sign-in by code clears it and
        works as before outside the Mini App. Without the marker a valid Mini App session
        wins when both cookies are present."""
        request.state.tma = False
        request.state.session_token = None
        tma_token = request.cookies.get(tma_cookie)
        if tma_token:
            info = await auth.session_for(tma_token)
            if info is not None:
                request.state.tma = True
                request.state.session_token = tma_token
                return info
        if request.cookies.get(tma_hint) == "1":
            return None
        token = request.cookies.get(SESSION_COOKIE)
        info = await auth.session_for(token)
        if info is not None:
            request.state.session_token = token
        return info

    async def require_session(request: Request) -> SessionInfo:
        info = await current_session(request)
        if info is None:
            raise NotLoggedIn()
        request.state.session = info
        return info

    async def guarded_form(request: Request) -> FormData:
        """Every state-changing request: origin + session + CSRF token."""
        if not origin_ok(request):
            raise Forbidden()
        info = await require_session(request)
        form = await request.form()
        if not same_token(
            form.get("csrf") if isinstance(form.get("csrf"), str) else None, info.csrf
        ):
            raise Forbidden()
        return form

    async def read(fn: Callable[[Uow], Awaitable[Any]]) -> Any:
        return await core.read(fn)

    async def flash(request: Request) -> dict[str, Any] | None:
        command_id = request.query_params.get("done")
        if not command_id or not command_id.startswith("web:"):
            return None
        async with core.sessions() as session:
            receipt = await session.get(CommandReceipt, command_id)
        if receipt is None or receipt.source != "web":
            return None
        result = receipt.result or {}
        return {
            "status": receipt.status,
            "message": result.get("message", ""),
            "command_id": receipt.command_id,
            "undoable": receipt.undoable and receipt.undone_by is None,
        }

    async def render(
        request: Request, name: str, context: dict[str, Any], status_code: int = 200
    ) -> HTMLResponse:
        info: SessionInfo | None = getattr(request.state, "session", None)
        today = await read(lambda uow: _today(uow))
        ctx = {
            "csrf": info.csrf if info else "",
            "logged_in": info is not None,
            "tma": bool(getattr(request.state, "tma", False)),
            "tma_entry": False,
            "path": request.url.path,
            "flash": await flash(request) if info else None,
            "idem": lambda: secrets.token_hex(16),
            "today": today,
            "today_text": f"{fmt_date(today)}, {WEEKDAYS_FULL[today.weekday()]}",
            "values": {},
            "errors": [],
            **context,
        }
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    async def execute(form: FormData, command: c._Command) -> CommandResult:
        idem = form.get("idem")
        if not isinstance(idem, str) or not forms.IDEM.match(idem):
            raise forms.FormError(["Форма устарела: обнови страницу и повтори."])
        ctx = CommandContext(command_id=f"web:{idem}", source="web", received_at=core.clock.now())
        result = await core.execute(command, ctx)  # type: ignore[arg-type]
        if result.code == "idempotency_conflict":  # a reused key with different content
            raise forms.FormError([result.message])
        log.info("web command %s %s replayed=%s", command.type, result.status, result.replayed)  # type: ignore[attr-defined]
        return result

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    async def act(request: Request, build: Callable[[FormData], Any], back: str) -> Response:
        """Card buttons: build, execute, show the stored result on the card."""
        form = await guarded_form(request)
        try:
            result = await execute(form, build(form))
        except forms.FormError as exc:
            return await render(
                request,
                "error.html",
                {
                    "title": "Не получилось",
                    "errors": exc.errors,
                    "back": safe_path(form.get("back"), back),
                },
                422,
            )
        return redirect(with_done(safe_path(form.get("back"), back), result.command_id))

    async def tz_today() -> tuple[Any, date]:
        return await read(lambda uow: _tz_today(uow))

    async def load(model: type[Any], entity_id: str) -> Any:
        async with core.sessions() as session:
            entity = await session.get(model, entity_id)
        if entity is None:
            raise NotFoundPage()
        return entity

    async def rule_of(entity: Task | Obligation) -> Recurrence | None:
        if entity.recurrence_id is None:
            return None
        return await load(Recurrence, entity.recurrence_id)

    # --- errors ------------------------------------------------------------------------

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn) -> Response:
        if request.method == "GET":
            if opts.tma is not None and request.cookies.get(tma_hint) == "1":
                # The Mini App signs in again through /tma (never through /login, which
                # would be a dead end inside Telegram); the page asked for is remembered.
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                return redirect(f"/tma?next={quote(safe_path(target, '/'), safe='')}")
            return redirect("/login")
        return HTMLResponse("Сессия истекла: войди заново.", status_code=401)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        log.warning("web: rejected %s %s (origin/csrf)", request.method, request.url.path)
        return HTMLResponse(
            "Запрос отклонён: проверка источника не прошла. Обнови страницу и повтори.",
            status_code=403,
        )

    @app.exception_handler(NotFoundPage)
    async def not_found(request: Request, exc: NotFoundPage) -> Response:
        return await render(
            request,
            "error.html",
            {"title": "Не найдено", "errors": ["Такой записи нет."], "back": "/"},
            404,
        )

    # --- health ------------------------------------------------------------------------

    def runtime_state() -> str:
        task = app.state.runtime_task
        if opts.runtime is None:
            return "disabled"
        return "running" if task is not None and not task.done() else "stopped"

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        checks: dict[str, Any] = {}
        try:
            async with core.sessions() as session:
                await session.execute(text("SELECT 1"))
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            checks["database"] = True
            checks["schema"] = revision == head
        except Exception:  # readiness reports, never raises
            checks["database"] = False
            checks["schema"] = False
        checks["background"] = runtime_state()
        ok = checks["database"] and checks["schema"] and checks["background"] != "stopped"
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    # --- login -------------------------------------------------------------------------

    def set_login_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            LOGIN_COOKIE,
            token,
            max_age=900,
            httponly=True,
            secure=opts.secure_cookies,
            samesite="strict",
            path="/login",
        )

    async def login_page(request: Request, error: str | None, status_code: int) -> Response:
        token = secrets.token_urlsafe(24)
        response = await render(
            request, "login.html", {"login_csrf": token, "error": error}, status_code
        )
        set_login_cookie(response, token)
        return response

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        if await current_session(request) is not None:
            return redirect("/")
        return await login_page(request, None, 200)

    @app.post("/login")
    async def login(request: Request) -> Response:
        if not origin_ok(request):
            raise Forbidden()
        form = await request.form()
        token = form.get("login_csrf")
        if not same_token(
            token if isinstance(token, str) else None, request.cookies.get(LOGIN_COOKIE)
        ):
            raise Forbidden()
        code = form.get("code")
        if not isinstance(code, str) or not 1 <= len(code) <= 32:
            return await login_page(request, "Введи код из Telegram.", 400)
        ip = request.client.host if request.client else "unknown"
        try:
            session_token = await auth.login(code, ip)
        except LoginThrottled:
            log.warning("web login throttled")
            return await login_page(
                request, "Слишком много неудачных попыток. Подожди 15 минут.", 429
            )
        except ValueError:
            log.info("web login failed")
            return await login_page(
                request, "Код не подошёл или устарел. Запроси новый: /login в Telegram.", 400
            )
        log.info("web login ok")
        response = redirect("/")
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=int(auth.session_ttl.total_seconds()),
            httponly=True,
            secure=opts.secure_cookies,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(LOGIN_COOKIE, path="/login")
        forget_tma_hint(response, tma_hint)  # a regular web sign-in: not a Mini App context
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        await guarded_form(request)
        token = request.state.session_token  # the session this very request used
        if token:
            await auth.logout(token)
        if request.state.tma:
            # No automatic sign-in again: /tma?bye=1 says the session ended.
            response = redirect("/tma?bye=1")
            forget_tma_cookie(response, tma_cookie)
        else:
            response = redirect("/login")
            response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/sessions/revoke-all")
    async def revoke_all(request: Request) -> Response:
        await guarded_form(request)
        await auth.revoke_all()  # every stored session: both ways of signing in
        response = redirect("/tma?bye=1" if request.state.tma else "/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        forget_tma_cookie(response, tma_cookie)
        return response

    # --- Telegram Mini App ---------------------------------------------------------------

    def tma_enabled() -> TmaConfig:
        if opts.tma is None:
            raise NotFoundPage()
        return opts.tma

    def tma_json(status: int, **body: Any) -> JSONResponse:
        return JSONResponse(body, status_code=status)

    @app.get("/tma")
    async def tma_page(request: Request) -> Response:
        # Never a redirect on the strength of a cookie: every open through /tma passes the
        # signed-identity check (POST /tma/auth), a valid old session or not.
        tma_enabled()
        return await render(
            request,
            "tma.html",
            {
                "tma": True,
                "tma_entry": True,
                "bye": request.query_params.get("bye") == "1",
                "next": safe_path(request.query_params.get("next"), "/"),
            },
        )

    @app.get("/tma/session")
    async def tma_session(request: Request) -> Response:
        """Lets the entry page confirm that the browser kept the cookie (no redirect loops)."""
        tma_enabled()
        info = await current_session(request)
        if info is None or not request.state.tma:
            return tma_json(401, ok=False)
        return tma_json(200, ok=True)

    async def read_limited(request: Request) -> bytes | None:
        """The body, or None when it exceeds the limit (checked before any parsing)."""
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return None
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/tma/auth")
    async def tma_auth(request: Request) -> Response:
        config = tma_enabled()
        if not origin_ok(request):
            raise Forbidden()
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return tma_json(415, ok=False, error="bad_request")
        body = await read_limited(request)
        if body is None:
            return tma_json(413, ok=False, error="too_large")
        ip = request.client.host if request.client else "unknown"
        # The identity check always runs (the body is already size-limited). The throttle only
        # decides whether a new session may be issued: an exhausted limit must not keep a
        # signed message of another account from ending the old session.
        throttled = await auth.tma_throttled(ip)
        try:
            payload = json.loads(body)
            raw = payload.get("init_data") if isinstance(payload, dict) else None
            if not isinstance(raw, str):
                raise InitDataError("malformed")
            validate_init_data(raw, config.bot_token, config.owner_id, core.clock.now())
        except (ValueError, InitDataError) as exc:
            code = exc.code if isinstance(exc, InitDataError) else "malformed"
            if code == "forbidden":
                # Telegram vouched for someone else: this context's old session ends. Input
                # that is not signed (or only stale) proves nothing and revokes nothing.
                previous = request.cookies.get(tma_cookie)
                if previous:
                    await auth.logout(previous)
            if not throttled and code not in ("stale", "future"):  # an old window is no attack
                await auth.tma_failed(ip)
            log.info("tma sign-in refused: %s", code)  # a fixed word, never the input
            if throttled and code != "forbidden":
                refused = tma_json(429, ok=False, error="throttled")
            else:
                status = 403 if code == "forbidden" else 401
                error = code if code in ("stale", "future") else "invalid"
                refused = tma_json(status, ok=False, error=error)
            if code == "forbidden":
                forget_tma_cookie(refused, tma_cookie)
            return refused
        if throttled:
            log.warning("tma sign-in throttled")
            return tma_json(429, ok=False, error="throttled")
        try:
            token = await auth.open_tma_session(ip, replace=request.cookies.get(tma_cookie))
        except LoginThrottled:  # the atomic check inside the insert said no
            log.warning("tma sign-in throttled")
            return tma_json(429, ok=False, error="throttled")
        log.info("tma sign-in ok")
        response = tma_json(200, ok=True)
        remember_tma_cookie(response, tma_cookie, token, auth.tma_ttl_seconds)
        remember_tma_hint(response, tma_hint)
        return response

    # --- lists -------------------------------------------------------------------------

    @app.get("/")
    async def today_page(request: Request) -> Response:
        await require_session(request)
        home = await read(views.home_view)
        return await render(request, "today.html", {"home": home})

    @app.get("/payments")
    async def payments_page(request: Request) -> Response:
        await require_session(request)
        rows = await read(views.payments_view)
        return await render(request, "payments.html", {"rows": rows})

    @app.get("/items")
    async def items_page(request: Request) -> Response:
        await require_session(request)
        query = (request.query_params.get("q") or "").strip()[:100] or None
        items = await read(lambda uow: views.items_view(uow, query))
        return await render(request, "items.html", {"items": items, "q": query or ""})

    @app.get("/history")
    async def history_page(request: Request) -> Response:
        await require_session(request)
        rows = await read(views.recent_receipts)
        return await render(request, "history.html", {"rows": rows})

    @app.get("/diagnostics")
    async def diagnostics_page(request: Request) -> Response:
        await require_session(request)
        sessions = await auth.active_sessions()
        async with core.sessions() as session:
            schema = await session.scalar(text("SELECT version_num FROM alembic_version"))
        diag = await read(
            lambda uow: views.diagnostics(
                uow,
                runtime_state=runtime_state(),
                llm=opts.llm,
                sessions=sessions,
                schema=schema,
                head=head,
                backups=opts.backup_dir is not None,
            )
        )
        return await render(request, "diagnostics.html", {"diag": diag})

    # --- tasks -------------------------------------------------------------------------

    @app.get("/tasks/new")
    async def task_new(request: Request) -> Response:
        await require_session(request)
        return await render(request, "task_form.html", {"task": None, "rule": None})

    @app.post("/tasks")
    async def task_create(request: Request) -> Response:
        form = await guarded_form(request)
        tz, today = await tz_today()
        back = safe_path(form.get("back"), "")
        try:
            result = await execute(form, forms.create_task(form, tz, today))
        except forms.FormError as exc:
            if back:
                return await render(
                    request,
                    "error.html",
                    {"title": "Не записал", "errors": exc.errors, "back": back},
                    422,
                )
            return await render(
                request,
                "task_form.html",
                {"task": None, "rule": None, "values": form, "errors": exc.errors},
                422,
            )
        if not result.ok and not back:
            return await render(
                request,
                "task_form.html",
                {"task": None, "rule": None, "values": form, "errors": [result.message]},
                409,
            )
        target = back or (f"/tasks/{result.data['task_id']}" if result.ok else "/")
        return redirect(with_done(target, result.command_id))

    @app.get("/tasks/{task_id}")
    async def task_page(request: Request, task_id: str) -> Response:
        await require_session(request)
        card = await read(lambda uow: views.task_card(uow, task_id))
        if card is None:
            raise NotFoundPage()
        settings = await read(lambda uow: _settings(uow))
        return await render(request, "task.html", {"card": card, "settings": settings})

    @app.get("/tasks/{task_id}/edit")
    async def task_edit(request: Request, task_id: str) -> Response:
        await require_session(request)
        task = await load(Task, task_id)
        tz, _ = await tz_today()
        return await render(
            request,
            "task_form.html",
            {"task": task, "rule": await rule_of(task), "values": _task_values(task, tz)},
        )

    @app.post("/tasks/{task_id}/edit")
    async def task_update(request: Request, task_id: str) -> Response:
        form = await guarded_form(request)
        task = await load(Task, task_id)
        rule = await rule_of(task)
        tz, _ = await tz_today()
        try:
            result = await execute(form, forms.update_task(form, tz, task, rule))
        except forms.FormError as exc:
            return await render(
                request,
                "task_form.html",
                {"task": task, "rule": rule, "values": form, "errors": exc.errors},
                422,
            )
        if not result.ok:
            return await render(
                request,
                "task_form.html",
                {"task": task, "rule": rule, "values": form, "errors": [result.message]},
                409,
            )
        return redirect(with_done(f"/tasks/{task_id}", result.command_id))

    def version_of(form: FormData) -> int:
        value = form.get("version")
        if not isinstance(value, str) or not value.isdigit():
            raise forms.FormError(["Форма устарела: обнови страницу."])
        return int(value)

    def occ_ref(form: FormData) -> tuple[str | None, int | None]:
        value = form.get("occ")
        if not isinstance(value, str) or not value:
            return None, None
        occ_id, _, version = value.partition(":")
        if not version.isdigit():
            raise forms.FormError(["Период: неверная отметка."])
        return occ_id, int(version)

    @app.post("/tasks/{task_id}/complete")
    async def task_complete(request: Request, task_id: str) -> Response:
        def build(form: FormData) -> c.CompleteTask:
            occ_id, occ_version = occ_ref(form)
            return forms.build(
                c.CompleteTask,
                task_id=task_id,
                expected_version=version_of(form),
                occurrence_id=occ_id,
                occurrence_version=occ_version,
            )

        return await act(request, build, f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/cancel")
    async def task_cancel(request: Request, task_id: str) -> Response:
        return await act(
            request,
            lambda form: forms.build(
                c.CancelTask, task_id=task_id, expected_version=version_of(form)
            ),
            f"/tasks/{task_id}",
        )

    async def snooze_builder(subject_type: str, subject_id: str) -> Callable[[FormData], Any]:
        settings = await read(lambda uow: _settings(uow))
        tz = get_zone(settings.timezone)

        def build(form: FormData) -> c.SnoozeNotification:
            parsed = forms.Form(form, tz)
            custom = parsed.instant("custom_date", "custom_time", "Когда напомнить")
            parsed.check()
            until = forms.snooze_until(
                parsed.text("choice"),
                custom,
                now=core.clock.now(),
                later_minutes=settings.later_minutes,
                evening=settings.evening_time,
                morning=settings.default_notice_time,
                tz=tz,
            )
            occ_id, _ = occ_ref(form)
            return forms.build(
                c.SnoozeNotification,
                subject_type=subject_type,
                subject_id=subject_id,
                occurrence_id=occ_id,
                until=until,
            )

        return build

    @app.post("/tasks/{task_id}/snooze")
    async def task_snooze(request: Request, task_id: str) -> Response:
        return await act(request, await snooze_builder("task", task_id), f"/tasks/{task_id}")

    @app.post("/obligations/{obligation_id}/snooze")
    async def obligation_snooze(request: Request, obligation_id: str) -> Response:
        return await act(
            request,
            await snooze_builder("obligation", obligation_id),
            f"/obligations/{obligation_id}",
        )

    # --- occurrences -------------------------------------------------------------------

    async def owner_url(occurrence_id: str) -> str:
        occ = await load(Occurrence, occurrence_id)
        prefix = "tasks" if occ.owner_type == "task" else "obligations"
        return f"/{prefix}/{occ.owner_id}"

    @app.post("/occurrences/{occurrence_id}/skip")
    async def occurrence_skip(request: Request, occurrence_id: str) -> Response:
        back = await owner_url(occurrence_id)
        return await act(
            request,
            lambda form: forms.build(
                c.SkipOccurrence, occurrence_id=occurrence_id, expected_version=version_of(form)
            ),
            back,
        )

    @app.post("/occurrences/{occurrence_id}/reschedule")
    async def occurrence_reschedule(request: Request, occurrence_id: str) -> Response:
        back = await owner_url(occurrence_id)

        def build(form: FormData) -> c.RescheduleOccurrence:
            parsed = forms.Form(form, get_zone("UTC"))  # a plain date, no instants
            new_due = parsed.date("new_due", "Новая дата")
            if new_due is None:
                parsed.errors.append("Новая дата: обязательно")
            parsed.check()
            return forms.build(
                c.RescheduleOccurrence,
                occurrence_id=occurrence_id,
                expected_version=version_of(form),
                new_due=new_due,
            )

        return await act(request, build, back)

    # --- obligations -------------------------------------------------------------------

    @app.get("/obligations/new")
    async def obligation_new(request: Request) -> Response:
        await require_session(request)
        kind = request.query_params.get("kind")
        values = {"kind": kind} if kind in ("calendar", "coverage", "balance") else {}
        return await render(
            request, "obligation_form.html", {"ob": None, "rule": None, "values": values}
        )

    @app.post("/obligations")
    async def obligation_create(request: Request) -> Response:
        form = await guarded_form(request)
        tz, today = await tz_today()
        try:
            result = await execute(form, forms.create_obligation(form, tz, today))
        except forms.FormError as exc:
            return await render(
                request,
                "obligation_form.html",
                {"ob": None, "rule": None, "values": form, "errors": exc.errors},
                422,
            )
        if not result.ok:
            return await render(
                request,
                "obligation_form.html",
                {"ob": None, "rule": None, "values": form, "errors": [result.message]},
                409,
            )
        return redirect(
            with_done(f"/obligations/{result.data['obligation_id']}", result.command_id)
        )

    @app.get("/obligations/{obligation_id}")
    async def obligation_page(request: Request, obligation_id: str) -> Response:
        await require_session(request)
        card = await read(lambda uow: views.obligation_card(uow, obligation_id))
        if card is None:
            raise NotFoundPage()
        paid_last = paid_through(card.ob.coverage_until) if card.ob.coverage_until else None
        return await render(request, "obligation.html", {"card": card, "paid_last": paid_last})

    @app.get("/obligations/{obligation_id}/edit")
    async def obligation_edit(request: Request, obligation_id: str) -> Response:
        await require_session(request)
        ob = await load(Obligation, obligation_id)
        return await render(
            request,
            "obligation_form.html",
            {"ob": ob, "rule": await rule_of(ob), "values": _ob_values(ob)},
        )

    @app.post("/obligations/{obligation_id}/edit")
    async def obligation_update(request: Request, obligation_id: str) -> Response:
        form = await guarded_form(request)
        ob = await load(Obligation, obligation_id)
        rule = await rule_of(ob)
        tz, _ = await tz_today()
        try:
            result = await execute(form, forms.update_obligation(form, tz, ob, rule))
        except forms.FormError as exc:
            return await render(
                request,
                "obligation_form.html",
                {"ob": ob, "rule": rule, "values": form, "errors": exc.errors},
                422,
            )
        if not result.ok:
            return await render(
                request,
                "obligation_form.html",
                {"ob": ob, "rule": rule, "values": form, "errors": [result.message]},
                409,
            )
        return redirect(with_done(f"/obligations/{obligation_id}", result.command_id))

    def ob_action(name: str, builder: Callable[..., Any]) -> None:
        async def endpoint(request: Request, obligation_id: str) -> Response:
            ob = await load(Obligation, obligation_id)
            tz, today = await tz_today()
            return await act(
                request, lambda form: builder(form, tz, ob, today), f"/obligations/{obligation_id}"
            )

        endpoint.__name__ = f"obligation_{name}"
        app.post(f"/obligations/{{obligation_id}}/{name}")(endpoint)

    ob_action("pay", lambda form, tz, ob, today: forms.mark_payment(form, tz, ob, today))
    ob_action("extend", lambda form, tz, ob, today: forms.extend_coverage(form, tz, ob, today))
    ob_action(
        "topup", lambda form, tz, ob, today: forms.balance_event(form, tz, ob, "topup", today)
    )
    ob_action(
        "correct", lambda form, tz, ob, today: forms.balance_event(form, tz, ob, "correct", today)
    )
    ob_action("rate", lambda form, tz, ob, today: forms.balance_event(form, tz, ob, "rate", today))
    ob_action(
        "archive",
        lambda form, tz, ob, today: forms.build(
            c.ArchiveObligation,
            obligation_id=ob.id,
            expected_version=version_of(form),
            outstanding=form.get("outstanding") or None,
        ),
    )

    # --- undo, settings ----------------------------------------------------------------

    @app.post("/undo")
    async def undo(request: Request) -> Response:
        def build(form: FormData) -> c.UndoCommand:
            target = form.get("target")
            return forms.build(
                c.UndoCommand,
                target_command_id=target if isinstance(target, str) and target else None,
            )

        return await act(request, build, "/history")

    @app.get("/settings")
    async def settings_page(request: Request) -> Response:
        await require_session(request)
        settings = await read(lambda uow: _settings(uow))
        return await render(
            request,
            "settings.html",
            {"settings": settings, "sessions": await auth.active_sessions()},
        )

    @app.post("/settings")
    async def settings_update(request: Request) -> Response:
        form = await guarded_form(request)
        settings = await read(lambda uow: _settings(uow))
        tz = get_zone(settings.timezone)
        try:
            result = await execute(form, forms.update_settings(form, tz, settings))
        except forms.FormError as exc:
            return await render(
                request,
                "settings.html",
                {
                    "settings": settings,
                    "values": form,
                    "errors": exc.errors,
                    "sessions": await auth.active_sessions(),
                },
                422,
            )
        return redirect(with_done("/settings", result.command_id))

    @app.post("/settings/timezone")
    async def timezone_update(request: Request) -> Response:
        return await act(
            request,
            lambda form: forms.build(
                c.ChangeTimezone,
                timezone=(form.get("timezone") or "").strip(),
                expected_version=version_of(form),
            ),
            "/settings",
        )

    return app


# --- small read helpers (run inside Application.read) ----------------------------------


async def _today(uow: Uow) -> date:
    return uow.today_now


async def _tz_today(uow: Uow) -> tuple[Any, date]:
    return uow.tz, uow.today_now


async def _settings(uow: Uow) -> Any:
    settings = uow.settings
    uow.session.expunge(settings)
    return settings


def _task_values(task: Task, tz: Any) -> dict[str, Any]:
    local = task.due_at.astimezone(tz) if task.due_at else None
    return {
        "title": task.title,
        "note": task.note or "",
        "url": task.url or "",
        "next_action": task.next_action or "",
        "due_date": (local.date() if local else task.due_date or "")
        and str(local.date() if local else task.due_date),
        "due_time": local.strftime("%H:%M") if local else "",
    }


def _ob_values(ob: Obligation) -> dict[str, Any]:
    amount = "" if ob.amount_minor is None else minor_to_input(ob.amount_minor, ob.currency)
    return {
        "title": ob.title,
        "aliases": ", ".join(ob.aliases or []),
        "amount": amount,
        "note": ob.note or "",
        "kind": ob.kind,
        "currency": ob.currency,
    }
