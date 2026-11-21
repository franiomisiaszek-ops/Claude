"""The web app: a small server for the iPhone (and the PC), behind a login.

Run with `papertrader web serve`. It listens on this computer only; your iPhone reaches
it privately through Tailscale, which also gives it HTTPS (docs/WEBAPP.md). Pages only
read the database, through connections that refuse writes. The three buttons that change
something (pause, resume, skip) each need a second confirmation and are logged.

Security, in short: nothing but the login page, the icons and the offline page is
visible without a valid session cookie; the cookie is HttpOnly, Secure and signed; forms
carry a CSRF token; wrong passwords lock the login for a growing time; only the host
names in `web.allowed_hosts` are answered; strict security headers on every response;
no API keys anywhere in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time as clock
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from papertrader import __version__
from papertrader.config import AppConfig, Settings, load_config
from papertrader.db.types import utcnow
from papertrader.safety import ALPACA_PAPER_BASE_URL
from papertrader.web import actions, auth
from papertrader.web import data as d

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
TEMPLATES = HERE / "templates"
FLASH = "pt_flash"
REFRESH_SECONDS = 300  # look again for systems that were not set up
COMPARE_CACHE_SECONDS = 120
MAX_BODY = 16 * 1024
DASH = "\u2013"  # shown for a missing number

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'; object-src 'none'")  # fmt: skip
HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),  # other sites never see where you came from
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"strict-transport-security", b"max-age=31536000"),
]
TABS = (("today", "/", "Today"), ("compare", "/compare", "Compare"), ("trades", "/trades", "Trades"),
        ("planned", "/planned", "Planned"), ("more", "/more", "More"))  # fmt: skip
MORE = (("/c", "C portfolio", "C's weekly plan, the Avanza list, its backtest"),
        ("/decisions", "Decisions and reports", "the judge's ideas and every agent report with sources"),
        ("/news", "News on held stocks", "the latest headlines for every stock held"),
        ("/postmortems", "Post-mortems", "system B's \"why did it move?\" cases"),
        ("/agents", "Agents", "who was right, weights and lessons"),
        ("/model", "Pattern model", "its versions and test results"),
        ("/costs", "Costs", "Claude spending against each budget"),
        ("/jobs", "Jobs and messages", "the schedule's runs and Telegram messages"),
        ("/settings", "Settings and buttons", "pause or resume a system, the activity log"))  # fmt: skip


class SecurityHeaders:
    """Adds the security headers to every response; pages are never cached."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        static = str(scope.get("path", "")).startswith("/static/")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    h for h in message.get("headers", []) if h[0].lower() != b"cache-control"
                ]
                headers += HEADERS
                headers.append(
                    (b"cache-control", b"public, max-age=3600" if static else b"no-store")
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


@dataclass
class State:
    settings: Settings
    config: AppConfig
    folder: Path
    now: Callable[[], datetime]
    lockout: auth.Lockout
    views: dict[str, Any] = field(default_factory=dict)
    views_at: float = 0.0
    db_error: str | None = None
    compare_cache: tuple[float, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def auth_file(self) -> Path:
        return auth.auth_path(self.data_dir)

    def get_views(self) -> dict[str, Any]:
        """The systems' read-only connections, opened on first use and refreshed."""
        from papertrader.hub import open_views

        with self.lock:
            stale = clock.monotonic() - self.views_at > REFRESH_SECONDS
            if (not self.views or (len(self.views) < 3 and stale)) and (stale or not self.views_at):
                self.views_at = clock.monotonic()
                try:
                    found = open_views(
                        self.settings, self.folder, skip=set(self.views), read_only=True
                    )
                    self.views = {**self.views, **{str(k): v for k, v in found.items()}}
                    self.db_error = None
                except Exception as exc:  # the page explains it; the app keeps running
                    self.db_error = f"cannot reach the database ({type(exc).__name__})"
            return dict(self.views)


def _templates() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]), trim_blocks=True,
                      lstrip_blocks=True)  # fmt: skip
    env.filters.update(money=money, pct=pct, num=num, signed_money=signed_money, cls=sign_class)
    env.globals.update(TITLES=d.TITLES, TABS=TABS, MORE=MORE, version=__version__, paper=ALPACA_PAPER_BASE_URL,
                       SLOTS={"A": 0, "B": 1, "C": 2, "SPY": 3}, asset_v=_asset_version())  # fmt: skip
    return Jinja2Templates(env=env)


def _asset_version() -> str:
    """Changes whenever the style or script changes, so phones fetch the new one."""
    digest = hashlib.sha256()
    for name in ("app.css", "app.js"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:10]


# --------------------------------------------------------------------------- formatting


def money(value: float | None, digits: int = 2) -> str:
    return DASH if value is None else f"${value:,.{digits}f}"


def signed_money(value: float | None) -> str:
    return DASH if value is None else f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def pct(value: float | None, digits: int = 1, signed: bool = True) -> str:
    if value is None:
        return DASH
    return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return DASH
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"


def sign_class(value: float | None) -> str:
    if value is None or abs(value) < 1e-12:
        return ""
    return "up" if value > 0 else "down"


# --------------------------------------------------------------------------- the app


def create_app(settings: Settings | None = None, config: AppConfig | None = None, *,
               folder: Path = Path("config"), now: Callable[[], datetime] = utcnow) -> Starlette:  # fmt: skip
    settings = settings or Settings()
    config = config or load_config(settings.papertrader_config)
    state = State(
        settings,
        config,
        folder,
        now,
        auth.Lockout(config.web.lockout_after, config.web.lockout_max_minutes),
    )
    templates = _templates()

    # ------------------------------------------------------------------ helpers

    def session_token(request: Request) -> str | None:
        token = request.cookies.get(auth.COOKIE)
        record = auth.load(state.auth_file)
        return token if auth.valid_session(record, token, state.now()) else None

    def same_origin(request: Request) -> bool:
        """False only when the browser says the form came from another site. (Forms that
        change something also need the CSRF token, so a missing Origin is not enough.)"""
        origin = request.headers.get("origin")
        if not origin or origin == "null":
            return True
        host = request.headers.get("host", "")
        return origin.split("://", 1)[-1] == host

    def render(
        request: Request,
        name: str,
        context: dict[str, Any],
        token: str | None,
        *,
        status: int = 200,
    ) -> Response:
        record = auth.load(state.auth_file)
        flash = request.cookies.get(FLASH)
        response = templates.TemplateResponse(request, name, {
            "csrf": auth.csrf_token(record, token) if record and token else "", "flash": flash,
            "db_error": state.db_error, "now": d.ny(state.now(), "%a %d %b %H:%M"), "authed": bool(token), **context,
        }, status_code=status)  # fmt: skip
        if flash:
            response.delete_cookie(FLASH, path="/")
        return response

    def page(
        tab: str, template: str, build: Callable[[Request, dict[str, Any]], dict[str, Any]]
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            token = session_token(request)
            if token is None:
                return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
            views = await run_in_threadpool(state.get_views)
            try:
                context = await run_in_threadpool(build, request, views)
            except Exception as exc:  # one broken query must not show a stack trace
                context = {
                    "error": f"This page could not be built ({type(exc).__name__}). The logs have the details."
                }
                import logging

                logging.getLogger(__name__).exception("web page failed", extra={"page": tab})
            return render(request, template, {"tab": tab, "views": views, **context}, token)

        return endpoint

    def system_arg(
        request: Request, views: dict[str, Any], allowed: tuple[str, ...] = ("A", "B", "C")
    ) -> str | None:
        wanted = request.query_params.get("s", "").upper()
        present = [n for n in allowed if n in views]
        return wanted if wanted in present else (present[0] if present else None)

    # ------------------------------------------------------------------ pages

    def build_today(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {"today": d.today(views, state.now()), "autorefresh": 60}

    def build_compare(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        cached = state.compare_cache
        if cached and clock.monotonic() - cached[0] < COMPARE_CACHE_SECONDS:
            result = cached[1]
        else:
            result = (d.compare(views, state.config, state.now()), d.since_first_day(views))
            state.compare_cache = (clock.monotonic(), result)
        return {"cmp": result[0], "first": result[1]}

    def build_trades(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {"systems": d.trades(views, state.now()), "autorefresh": 120}

    def build_planned(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        items, cplan = d.planned(views, state.now())
        return {"items": items, "cplan": cplan, "autorefresh": 120}

    def build_c(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        _, cplan = d.planned(views, state.now())
        equity = d.equity_series(views["C"]) if "C" in views else []
        return {"cplan": cplan, "extras": d.c_extras(views), "equity": equity,
                "portfolio": views["C"].config.portfolio if "C" in views else None}  # fmt: skip

    def build_decisions(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        system = system_arg(request, views, ("A", "B"))
        if system is None:
            return {"system": None, "systems": []}
        raw = request.query_params.get("id", "")
        choices, view = d.decisions(views[system], int(raw) if raw.isdigit() else None)
        ticker = request.query_params.get("t", "").upper()
        return {"system": system, "systems": [n for n in ("A", "B") if n in views], "choices": choices, "dv": view,
                "ticker": ticker if ticker.isalnum() else "", "safe_url": d.safe_url}  # fmt: skip

    def build_news(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {"news": d.news(views, state.now())}

    def build_postmortems(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {"cases": d.postmortems(views), "has_b": "B" in views}

    def build_agents(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        system = system_arg(request, views, ("A", "B"))
        window = 30 if request.query_params.get("w") == "30" else 90
        if system is None:
            return {"system": None, "systems": []}
        board, weights, lesson = d.agents(views[system], window)
        return {"system": system, "systems": [n for n in ("A", "B") if n in views], "board": board, "weights": weights,
                "lesson": lesson, "window": window}  # fmt: skip

    def build_model(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        system = system_arg(request, views, ("A", "B"))
        rows = d.model_versions(views[system]) if system else []
        return {"system": system, "systems": [n for n in ("A", "B") if n in views], "rows": rows}

    def build_costs(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {"costs": d.costs(views, state.now())}

    def build_jobs(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        system = system_arg(request, views)
        monitor = request.query_params.get("monitor") == "1"
        info = d.jobs(views[system], state.now(), monitor) if system else None
        return {"system": system, "systems": [n for n in ("A", "B", "C") if n in views], "info": info,
                "monitor": monitor}  # fmt: skip

    def build_settings(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        switches = {}
        for name, view in views.items():
            switches[name] = d.jobs(view, state.now(), False)
        return {"rows": {n: d.settings_rows(v) for n, v in views.items()}, "switches": switches,
                "tables": {n: d.table_counts(v) for n, v in views.items()},
                "audit": actions.recent(state.data_dir), "order": [n for n in d.SYSTEM_ORDER if n in views]}  # fmt: skip

    def build_more(request: Request, views: dict[str, Any]) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------------ login

    async def login(request: Request) -> Response:
        record = auth.load(state.auth_file)
        nxt = _safe_next(request.query_params.get("next", "/"))
        if request.method == "GET":
            if session_token(request):
                return RedirectResponse(nxt, status_code=303)
            return render(
                request,
                "login.html",
                {"tab": "", "setup": record is None, "next": nxt, "error": None},
                None,
            )
        if not same_origin(request):
            return PlainTextResponse("Refused: the form came from another site.", status_code=403)
        form = await request.form()
        nxt = _safe_next(str(form.get("next", "/")))
        password = str(form.get("password", ""))[:256]
        now = state.now()
        if record is None:
            return render(
                request, "login.html", {"tab": "", "setup": True, "next": nxt, "error": None}, None
            )
        wait = state.lockout.seconds_left(now)
        if wait:
            actions.audit(state.data_dir, now, "login_locked", seconds=wait)
            return render(request, "login.html", {"tab": "", "setup": False, "next": nxt,
                          "error": f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s)."}, None, status=429)  # fmt: skip
        if not await run_in_threadpool(auth.check_password, record, password):
            locked = state.lockout.failed(now)
            actions.audit(
                state.data_dir,
                now,
                "login_failed",
                failures=state.lockout.failures,
                locked_seconds=locked,
            )
            if locked:
                await run_in_threadpool(_alert_lockout, state, locked)
            await asyncio.sleep(1.0)  # slows down guessing
            return render(request, "login.html", {"tab": "", "setup": False, "next": nxt,
                          "error": "Wrong password." + (f" Logins are locked for {locked // 60} minute(s)." if locked else "")},
                          None, status=401)  # fmt: skip
        state.lockout.succeeded()
        actions.audit(
            state.data_dir, now, "login", agent=str(request.headers.get("user-agent", ""))[:80]
        )
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(auth.COOKIE, auth.make_session(record, now, state.config.web.session_days),
                            max_age=state.config.web.session_days * 86400, path="/", secure=True, httponly=True,
                            samesite="lax")  # fmt: skip
        return response

    async def logout(request: Request) -> Response:
        token = session_token(request)
        form = await request.form()
        if (
            token
            and same_origin(request)
            and auth.valid_csrf(auth.load(state.auth_file), token, str(form.get("csrf", "")))
        ):
            actions.audit(state.data_dir, state.now(), "logout")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    # ------------------------------------------------------------------ actions

    async def confirm(request: Request) -> Response:
        token = session_token(request)
        if token is None:
            return RedirectResponse("/login", status_code=303)
        q = request.query_params
        try:
            action = actions.parse(q.get("a", ""), q.get("s", ""), q.get("t", ""))
        except actions.ActionError as exc:
            return render(
                request,
                "confirm.html",
                {"tab": "more", "error": str(exc), "action": None},
                token,
                status=400,
            )
        back = "/planned" if action.kind == "skip" else "/settings"
        return render(
            request,
            "confirm.html",
            {"tab": "more", "action": action, "back": back, "error": None},
            token,
        )

    async def act(request: Request) -> Response:
        token = session_token(request)
        if token is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not same_origin(request) or not auth.valid_csrf(
            auth.load(state.auth_file), token, str(form.get("csrf", ""))
        ):
            return PlainTextResponse("Refused: the form has expired or came from another site. Go back and try again.",
                                     status_code=403)  # fmt: skip
        if form.get("confirm") != "yes":
            return RedirectResponse("/settings", status_code=303)
        try:
            action = actions.parse(
                str(form.get("a", "")), str(form.get("s", "")), str(form.get("t", ""))
            )
        except actions.ActionError as exc:
            return PlainTextResponse(f"Refused: {exc}.", status_code=400)
        now = state.now()
        try:
            message = await run_in_threadpool(actions.run, state.settings.database_url, action, now)
        except Exception as exc:
            message = f"That did not work ({type(exc).__name__}); nothing was changed."
        actions.audit(state.data_dir, now, "action", action=action.kind, system=action.system, ticker=action.ticker,
                      result=message)  # fmt: skip
        state.compare_cache = None
        response = RedirectResponse(
            "/planned" if action.kind == "skip" else "/settings", status_code=303
        )
        response.set_cookie(
            FLASH,
            message[:300],
            max_age=60,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    # ------------------------------------------------------------------ plumbing

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def manifest(request: Request) -> Response:
        return JSONResponse({
            "name": "papertrader", "short_name": "papertrader", "description": "Paper-trading experiment: A vs B vs C",
            "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
            "background_color": "#0d0d0d", "theme_color": "#1a1a19",
            "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                       "purpose": "maskable"}],
        }, media_type="application/manifest+json")  # fmt: skip

    async def service_worker(request: Request) -> Response:
        return FileResponse(
            STATIC / "sw.js", media_type="text/javascript", headers={"Service-Worker-Allowed": "/"}
        )

    async def offline(request: Request) -> Response:
        return render(request, "offline.html", {"tab": ""}, None)

    routes = [
        Route("/", page("today", "today.html", build_today)),
        Route("/compare", page("compare", "compare.html", build_compare)),
        Route("/trades", page("trades", "trades.html", build_trades)),
        Route("/planned", page("planned", "planned.html", build_planned)),
        Route("/c", page("more", "c.html", build_c)),
        Route("/decisions", page("more", "decisions.html", build_decisions)),
        Route("/news", page("more", "news.html", build_news)),
        Route("/postmortems", page("more", "postmortems.html", build_postmortems)),
        Route("/agents", page("more", "agents.html", build_agents)),
        Route("/model", page("more", "model.html", build_model)),
        Route("/costs", page("more", "costs.html", build_costs)),
        Route("/jobs", page("more", "jobs.html", build_jobs)),
        Route("/settings", page("more", "settings.html", build_settings)),
        Route("/more", page("more", "more.html", build_more)),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/confirm", confirm),
        Route("/action", act, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/manifest.webmanifest", manifest),
        Route("/sw.js", service_worker),
        Route("/offline", offline),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    middleware = [
        Middleware(SecurityHeaders),
        Middleware(
            TrustedHostMiddleware, allowed_hosts=config.web.allowed_hosts, www_redirect=False
        ),
    ]
    app = Starlette(routes=routes, middleware=middleware, max_body_size=MAX_BODY)
    app.state.web = state
    return app


def _safe_next(value: str) -> str:
    """Only paths on this site: never another site after logging in."""
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        return "/"
    return value[:200]


def _alert_lockout(state: State, seconds: int) -> None:
    """One Telegram message per lock, through system A's outbox (the hub sends it)."""
    from papertrader.db.session import make_session_factory, make_system_engine
    from papertrader.notify.outbox import queue

    try:
        engine = make_system_engine(state.settings.database_url, "A")
        try:
            with make_session_factory(engine)() as session:
                queue(session, "alert", f"WEB APP · {state.lockout.failures} wrong passwords in a row: logins are "
                      f"locked for {seconds // 60} minute(s). If this was not you, check who is on your Tailscale network.")  # fmt: skip
                session.commit()
        finally:
            engine.dispose()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("could not queue the lockout alert")
