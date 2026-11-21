"""The web app (X5): login, sessions, lockout, every page with data, the confirmed actions,
the security headers, and the hub's live snapshot. All with a test client; no browser."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from starlette.testclient import TestClient

from papertrader.b.models import IntradayPlan, PostMortem
from papertrader.c.models import BacktestRun, Order, Plan
from papertrader.config import Settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.db.state import TRADING_PAUSED_KEY, get_state, set_state
from papertrader.experiment.measure import set_start
from papertrader.ingest.timeutil import at_new_york
from papertrader.trading import control
from papertrader.trading.broker import Account, Position
from papertrader.web import actions, auth, live
from papertrader.web.app import _safe_next, create_app

from ..conftest import PROJECT_ROOT
from ..dashboard.test_dashboard import seed
from ..ingest.helpers import shipped_config
from ..trading.helpers import MONDAY

CFG = shipped_config()
PASSWORD = "correct horse battery staple"
NOW = at_new_york(MONDAY, time(9, 0))  # before B's planned trade and A's 09:35 orders
FOLDER = PROJECT_ROOT / "config"


@dataclass
class Clock:
    at: datetime = NOW

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def systems(engine: Engine, database_url: str) -> Iterator[dict[str, Engine]]:
    out = {"A": engine}
    for name in ("B", "C"):
        out[name] = make_system_engine(database_url, name)
        provision(out[name], name)
    yield out
    for name in ("B", "C"):
        out[name].dispose()


def settings_for(database_url: str, tmp_path: Path) -> Settings:
    return Settings(database_url=database_url, data_dir=tmp_path / "data")


def make_client(
    database_url: str, tmp_path: Path, clock: Clock | None = None, *, password: bool = True
) -> TestClient:
    settings = settings_for(database_url, tmp_path)
    if password:
        auth.set_password(auth.auth_path(settings.data_dir), PASSWORD, 16, NOW)
    app = create_app(settings, CFG, folder=FOLDER, now=clock or Clock())
    return TestClient(app, base_url="https://localhost")


def log_in(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def csrf(client: TestClient, path: str = "/settings") -> str:
    found = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get(path).text)
    assert found, "no CSRF token on the page"
    return found.group(1)


# --------------------------------------------------------------------------- auth, unit by unit


def test_passwords_sessions_and_csrf(tmp_path: Path) -> None:
    path = tmp_path / "web_auth.json"
    with pytest.raises(auth.PasswordError, match="at least 16"):
        auth.set_password(path, "short", 16, NOW)
    with pytest.raises(auth.PasswordError, match="too few different"):
        auth.set_password(path, "a" * 20, 16, NOW)
    record = auth.set_password(path, PASSWORD, 16, NOW)
    assert PASSWORD not in path.read_text() and (path.stat().st_mode & 0o777) == 0o600
    assert auth.check_password(record, PASSWORD) and not auth.check_password(record, PASSWORD + "!")

    token = auth.make_session(record, NOW, days=30)
    assert auth.valid_session(record, token, NOW + timedelta(days=29))
    assert not auth.valid_session(record, token, NOW + timedelta(days=31))  # expired
    issued, expires, sig = token.split(".")
    assert not auth.valid_session(
        record, f"{issued}.{int(expires) + 999999}.{sig}", NOW
    )  # tampered
    assert not auth.valid_session(record, "garbage", NOW) and not auth.valid_session(
        None, token, NOW
    )
    assert auth.valid_csrf(record, token, auth.csrf_token(record, token))
    assert not auth.valid_csrf(record, token, "0" * 40) and not auth.valid_csrf(record, None, "x")

    fresh = auth.logout_everywhere(path)  # a new secret: old sessions stop working
    assert fresh is not None and not auth.valid_session(fresh, token, NOW)
    assert auth.check_password(fresh, PASSWORD)  # the password is unchanged


def test_the_lockout_grows_and_resets() -> None:
    lock = auth.Lockout(after=5, max_minutes=60)
    assert [lock.failed(NOW) for _ in range(4)] == [0, 0, 0, 0]
    assert lock.failed(NOW) == 60  # 5th wrong password: 1 minute
    assert lock.seconds_left(NOW) > 0 and lock.seconds_left(NOW + timedelta(minutes=2)) == 0
    assert lock.failed(NOW) == 120 and lock.failed(NOW) == 240  # then 2, 4 ... minutes
    for _ in range(10):
        lock.failed(NOW)
    assert lock.failed(NOW) == 3600  # never more than the maximum
    lock.succeeded()
    assert lock.failures == 0 and lock.seconds_left(NOW) == 0


def test_after_login_only_this_site() -> None:
    assert _safe_next("/trades") == "/trades"
    for bad in ("//evil.example", "https://evil.example", "/\\evil", "evil", "/\nx"):
        assert _safe_next(bad) == "/"


# --------------------------------------------------------------------------- login


def test_nothing_is_visible_without_logging_in(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    client = make_client(database_url, tmp_path)
    for path in ("/", "/compare", "/trades", "/planned", "/c", "/decisions", "/news", "/postmortems",
                 "/agents", "/model", "/costs", "/jobs", "/settings", "/more", "/confirm?a=pause&s=A"):  # fmt: skip
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login"), (
            path
        )
    assert (
        client.post(
            "/action", data={"a": "pause", "s": "A", "confirm": "yes"}, follow_redirects=False
        ).status_code
        == 303
    )
    for public in ("/login", "/healthz", "/manifest.webmanifest", "/sw.js", "/offline", "/static/app.css",
                   "/static/icons/apple-touch-icon.png"):  # fmt: skip
        assert client.get(public).status_code == 200, public


def test_without_a_password_the_login_explains_how_to_set_one(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    client = make_client(database_url, tmp_path, password=False)
    page = client.get("/login").text
    assert "papertrader web set-password" in page and 'type="password"' not in page
    assert client.post("/login", data={"password": PASSWORD}).status_code == 200
    assert auth.COOKIE not in client.cookies


def test_wrong_passwords_lock_the_login_and_alert(engine: Engine, database_url: str, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr("papertrader.web.app.asyncio.sleep", _no_sleep)
    clock = Clock()
    client = make_client(database_url, tmp_path, clock)
    for _ in range(4):
        assert client.post("/login", data={"password": "wrong wrong wrong!"}).status_code == 401
    locked = client.post("/login", data={"password": "wrong wrong wrong!"})
    assert locked.status_code == 401 and "locked for 1 minute" in locked.text
    assert (
        client.post("/login", data={"password": PASSWORD}).status_code == 429
    )  # even the right one
    clock.at = NOW + timedelta(minutes=2)
    log_in(client)
    with make_session_factory(engine)() as session:
        alert = session.scalars(select(m.Notification).where(m.Notification.kind == "alert")).all()
    assert any("5 wrong passwords" in n.text for n in alert)
    events = [e["event"] for e in actions.recent(tmp_path / "data")]
    assert events[0] == "login" and events.count("login_failed") == 5 and "login_locked" in events


async def _no_sleep(_seconds: float) -> None:
    return None


def test_login_sets_a_safe_cookie_and_logout_clears_it(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    client = make_client(database_url, tmp_path)
    response = client.post(
        "/login", data={"password": PASSWORD, "next": "/trades"}, follow_redirects=False
    )
    assert response.headers["location"] == "/trades"
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", "max-age=2592000"))
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_forms_from_another_site_are_refused(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    client = make_client(database_url, tmp_path)
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- the pages, with data


def seed_all(systems: dict[str, Engine]) -> None:
    a, b, c = systems["A"], systems["B"], systems["C"]
    seed(
        a
    )  # a week of A: snapshots, trades (AAPL open), a decision with reports, scores, spend, jobs
    seed(b)
    at = NOW - timedelta(minutes=1)
    with make_session_factory(a)() as session:
        report = session.scalars(
            select(m.AgentReport).where(m.AgentReport.ticker == "AAPL")
        ).first()
        assert report is not None
        report.evidence = [{"claim": "Revenue beat", "source_id": "news:1", "url": "https://example.com/n1"},
                           {"claim": "Odd link", "source_id": "x:2", "url": "javascript:alert(1)"}]  # fmt: skip
        for i, url in enumerate(("https://example.com/aapl", "javascript:alert(2)")):
            news = m.NewsItem(source="alpaca", source_item_id=f"n{i}", headline=f"Apple headline {i}", summary="sum",
                              url=url, published_at=NOW - timedelta(hours=2 + i), available_at=NOW - timedelta(hours=2 + i),
                              content_hash=f"h{i}")  # fmt: skip
            news.tickers.append(m.NewsTicker(ticker="AAPL"))
            session.add(news)
        for i in range(6):
            day = MONDAY - timedelta(days=7 - i)
            session.add(m.PriceBar(ticker="SPY", timeframe="1Day", ts=at_new_york(day, time(9, 30)), open=600, high=601,
                                   low=599, close=600 + i, volume=1, available_at=at_new_york(day, time(16, 0))))  # fmt: skip
        set_state(
            session,
            live.LIVE_KEY,
            _snapshot(at, 101_900, 101_000, {"AAPL": (10, 200, 205)}, spy=606.0),
        )
        session.commit()
    set_start(a, MONDAY - timedelta(days=3), NOW)
    with make_session_factory(b)() as session:
        for status, ticker in (("planned", "NVDA"), ("shadow", "AMD")):
            session.add(IntradayPlan(day=MONDAY, created_at=at, mode="live" if status == "planned" else "shadow",
                                     status=status, ticker=ticker, rule="breakout", facts={}, price_at_decision=100,
                                     entry_low=99.5, entry_high=100.5, target_price=104, stop_price=98, size_pct=0.02,
                                     confidence=0.7, expected_hold_minutes=90, time_stop_at=NOW + timedelta(hours=3),
                                     place_after=NOW + timedelta(minutes=3), why="clean breakout on volume",
                                     invalidation="falls back under 99"))  # fmt: skip
        session.add(PostMortem(day=MONDAY - timedelta(days=3), created_at=at, kind="closed_trade", ticker="XOM",
                               period_start=MONDAY - timedelta(days=5), period_end=MONDAY - timedelta(days=3),
                               move_pct=0.06, move_vs_spy=0.05, what_b_did="bought", decision_quality="good",
                               outcome="good", causes=[{"cause": "oil rose", "likelihood": 0.6, "sources": ["news:7"]}],
                               lesson="energy follows oil", why_b_acted="strong report"))  # fmt: skip
        set_state(session, live.LIVE_KEY, _snapshot(at, 100_500, 100_000, {"AAPL": (10, 200, 205)}))
        session.commit()
    with make_session_factory(c)() as session:
        session.add(Plan(signal_day=MONDAY - timedelta(days=3), trade_day=MONDAY, status="planned", state="normal",
                         equity=25_000, drawdown=0.0, targets={"GLD": 0.2, "SPY": 0.15, "SGOV": 0.4},
                         shadow_targets={"GLD": 0.2, "SPY": 0.2, "SGOV": 0.35},
                         reasons={"GLD": "in: momentum +18.0%", "SPY": "in: momentum +12.0%"},
                         review={"SPY": {"verdict": "reduce", "reason": "Fed decision on Wednesday"}}))  # fmt: skip
        session.add(Order(kind="stop", ticker="GLD", side="sell", qty=10, stop_price=300.0, client_order_id="c-stop-1",
                          status="new"))  # fmt: skip
        session.add(Order(kind="rebalance", ticker="GLD", side="buy", qty=10, limit_price=376, client_order_id="c-1",
                          status="filled", filled_qty=10, filled_avg_price=375.0))  # fmt: skip
        session.add(BacktestRun(ran_at=NOW - timedelta(days=2), chosen={"top_n": 6}, verdict="C does NOT meet its target",
                                result={"c": {"in_sample": {"cagr": 0.035, "sharpe": 0.36, "max_drawdown": -0.123},
                                              "out_of_sample": {"cagr": 0.049, "sharpe": 0.39, "max_drawdown": -0.119}}}))  # fmt: skip
        set_state(session, live.LIVE_KEY, _snapshot(at, 25_100, 25_000, {"GLD": (10, 375, 380)}))
        session.commit()


def _snapshot(at: datetime, equity: float, last: float, positions: dict[str, tuple[float, float, float]],
              spy: float | None = None) -> dict[str, Any]:  # fmt: skip
    return {"at": at.isoformat(), "equity": equity, "last_equity": last, "cash": 1000.0, "spy": spy,
            "positions": [{"symbol": s, "qty": q, "avg_entry": e, "price": p} for s, (q, e, p) in positions.items()]}  # fmt: skip


def test_every_page_shows_the_three_systems(
    systems: dict[str, Engine], database_url: str, tmp_path: Path
) -> None:
    seed_all(systems)
    client = make_client(database_url, tmp_path)
    log_in(client)
    pages = {}
    for path in ("/", "/compare", "/trades", "/planned", "/c", "/decisions", "/decisions?s=B", "/news",
                 "/postmortems", "/agents", "/agents?s=B&w=30", "/model", "/costs", "/jobs?s=C&monitor=1",
                 "/settings", "/more"):  # fmt: skip
        response = client.get(path)
        assert response.status_code == 200, path
        assert "could not be built" not in response.text, (path, response.text[:2000])
        pages[path] = response.text
    today = pages["/"]
    assert "$101,900" in today and "live, 08:59" in today and "Paused: testing" in today  # A, live
    assert "C · ETF trend portfolio" in today and "+0.17%" in today  # SPY today: 606 vs 605
    assert "+0.89%" in today and "+0.73%" in today  # A today: 101,900 / 101,000, and that minus SPY
    trades = pages["/trades"]
    assert (
        "AAPL" in trades and "+$50.00" in trades and "time left" in trades
    )  # live P&L on A's open trade
    assert "40% of the account" not in trades and "stop $300.00" in trades  # C's resting stop
    planned = pages["/planned"]
    assert (
        "today 09:35" in planned and "information only (A is frozen)" in planned
    )  # A's idea, no button
    assert (
        "/confirm?a=skip&amp;s=B&amp;t=NVDA" in planned and "t=AMD" not in planned
    )  # only live plans
    assert "Fed decision on Wednesday" in planned
    c_page = pages["/c"]
    assert "iShares Physical Gold ETC (SGLN)" in c_page and "C does NOT meet its target" in c_page
    news = pages["/news"]
    assert 'href="https://example.com/aapl"' in news and "javascript:" not in news
    decisions = pages["/decisions"]
    assert 'href="https://example.com/n1"' in decisions and "javascript:" not in decisions
    assert 'rel="noopener noreferrer"' in decisions
    assert "energy follows oil" in pages["/postmortems"]
    assert (
        "Pause C" in pages["/settings"] and "Resume A" in pages["/settings"]
    )  # A and B are paused in the seed
    assert "Return since the start, after costs" in pages["/compare"]
    chart = re.search(r'data-chart="([^"]+)"', pages["/compare"])
    assert chart is not None and json.loads(chart.group(1).replace("&#34;", '"'))["series"]
    for page in pages.values():
        assert database_url not in page and "secret" not in page.lower()


def test_pages_read_through_connections_that_refuse_writes(systems: dict[str, Engine], database_url: str,
                                                           tmp_path: Path) -> None:  # fmt: skip
    client = make_client(database_url, tmp_path)
    log_in(client)
    client.get("/")
    views = client.app.state.web.get_views()  # type: ignore[attr-defined]
    assert sorted(views) == ["A", "B", "C"]
    for view in views.values():
        with pytest.raises(Exception), view.engine.begin() as conn:  # noqa: B017 - SQLite and PostgreSQL differ
            conn.execute(text("UPDATE system_state SET key = key"))


# --------------------------------------------------------------------------- actions


def test_pause_needs_a_confirmation_a_token_and_is_logged(systems: dict[str, Engine], database_url: str,
                                                          tmp_path: Path) -> None:  # fmt: skip
    client = make_client(database_url, tmp_path)
    log_in(client)
    confirm = client.get("/confirm?a=pause&s=B")
    assert "Pause system B" in confirm.text and 'value="yes"' in confirm.text
    assert "frozen comparison" in client.get("/confirm?a=pause&s=A").text  # a warning for A
    assert client.get("/confirm?a=skip&s=A&t=AAPL").status_code == 400
    token = csrf(client, "/confirm?a=pause&s=B")
    refused = client.post(
        "/action", data={"a": "pause", "s": "B", "confirm": "yes", "csrf": "0" * 40}
    )
    assert refused.status_code == 403 and control.paused(make_session_factory(systems["B"])) is None
    cross = client.post("/action", data={"a": "pause", "s": "B", "confirm": "yes", "csrf": token},
                        headers={"origin": "https://evil.example"})  # fmt: skip
    assert cross.status_code == 403
    done = client.post(
        "/action",
        data={"a": "pause", "s": "B", "confirm": "yes", "csrf": token},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/settings"
    with make_session_factory(systems["B"])() as session:
        assert (get_state(session, TRADING_PAUSED_KEY) or {})["reason"] == "paused from the web app"
        sent = session.scalars(select(m.Notification.text)).all()
    assert any("WEB APP · System B paused" in t for t in sent)
    assert "System B paused from the web app" in client.get("/settings").text  # the message, once
    log = actions.recent(tmp_path / "data")
    assert log[0]["event"] == "action" and log[0]["system"] == "B" and log[0]["action"] == "pause"
    resume = client.post("/action", data={"a": "resume", "s": "B", "confirm": "yes", "csrf": token})
    assert resume.status_code == 200 and control.paused(make_session_factory(systems["B"])) is None


def test_skip_cancels_bs_planned_trade(
    systems: dict[str, Engine], database_url: str, tmp_path: Path
) -> None:
    seed_all(systems)
    client = make_client(database_url, tmp_path)
    log_in(client)
    token = csrf(client, "/confirm?a=skip&s=B&t=NVDA")
    client.post(
        "/action", data={"a": "skip", "s": "B", "t": "NVDA", "confirm": "yes", "csrf": token}
    )
    with make_session_factory(systems["B"])() as session:
        plan = session.scalars(select(IntradayPlan).where(IntradayPlan.ticker == "NVDA")).one()
    assert plan.status == "skipped" and "skipped by you" in plan.note
    bad = client.post(
        "/action", data={"a": "skip", "s": "B", "t": "<script>", "confirm": "yes", "csrf": token}
    )
    assert bad.status_code == 400


# --------------------------------------------------------------------------- headers and hosts


def test_security_headers_and_unknown_hosts(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    client = make_client(database_url, tmp_path)
    log_in(client)
    page = client.get("/")
    headers = page.headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
    assert (
        headers["referrer-policy"] == "same-origin"
        and "max-age" in headers["strict-transport-security"]
    )
    assert "public" in client.get("/static/app.js").headers["cache-control"]
    assert " style=" not in page.text and "<script>" not in page.text  # nothing inline (CSP)
    ts = TestClient(client.app, base_url="https://my-pc.tail1234.ts.net")
    assert ts.get("/healthz").status_code == 200
    evil = TestClient(client.app, base_url="https://evil.example")
    assert evil.get("/healthz").status_code == 400
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and any(
        i.get("purpose") == "maskable" for i in manifest["icons"]
    )


# --------------------------------------------------------------------------- the hub's live snapshot


class FakeAccountBroker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def account(self) -> Account:
        if self.fail:
            raise ConnectionError("down")
        return Account(
            equity=25_500, last_equity=25_000, cash=500, status="ACTIVE", trading_blocked=False
        )

    def positions(self) -> list[Position]:
        return [Position("GLD", 10, 375, 3800, 380), Position("TLT", 0, 90, 0, 91)]

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return {"SPY": 610.0}


def test_the_hub_stores_live_numbers_for_the_web_app(systems: dict[str, Engine]) -> None:
    from papertrader.hub import SystemView

    views = {n: SystemView(n, CFG, e, make_session_factory(e)) for n, e in systems.items()}  # type: ignore[arg-type]
    brokers = {
        "A": lambda: FakeAccountBroker(fail=True),
        "B": lambda: None,
        "C": lambda: FakeAccountBroker(),
    }
    problems = live.write_all(views, brokers, NOW)
    assert problems == {"A": "ConnectionError"}  # one account failing does not stop the others
    snap = live.read(views["C"].sessions)
    assert (
        snap.equity == 25_500 and snap.spy == 610.0 and list(snap.positions) == ["GLD"]
    )  # empty ones dropped
    gld = snap.positions["GLD"]
    assert gld.pnl_usd == pytest.approx(50.0) and gld.pnl_pct == pytest.approx(380 / 375 - 1)
    assert snap.day_change_pct == pytest.approx(0.02) and snap.fresh(NOW + timedelta(minutes=10))
    assert not snap.fresh(NOW + timedelta(minutes=20))
    assert live.read(views["B"].sessions).at is None  # no broker, nothing stored


def test_the_web_app_holds_no_keys() -> None:
    """The web service in docker-compose.yml gets the database address only, never .env."""
    import yaml

    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    web = compose["services"]["web"]
    assert "env_file" not in web
    env = web.get("environment", {})
    assert not any(
        word in key for key in env for word in ("ALPACA", "ANTHROPIC", "TELEGRAM", "KEY", "TOKEN")
    )
    assert web["ports"] == ["127.0.0.1:8501:8501"]
    assert "dashboard" not in compose["services"]  # the web app replaced the Streamlit dashboard
