"""Streamlit entry point:  streamlit run src/papertrader/dashboard/app.py

Read-only: the database connection is opened in read-only mode, so this page
cannot change anything even by accident. Pages are picked in the sidebar.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from papertrader import __version__
from papertrader.config import AppConfig, ConfigError, get_config, get_settings
from papertrader.dashboard import charts, data
from papertrader.db.session import make_engine
from papertrader.safety import ALPACA_PAPER_BASE_URL

st.set_page_config(page_title="papertrader", layout="wide")

PAGES = ("Overview", "Experiment: A vs B vs C", "Equity vs SPY", "Trades", "Decisions and reports",
         "Agent leaderboard", "Pattern model", "API spend", "Jobs and messages", "Settings")  # fmt: skip


@st.cache_resource
def _engine(url: str):  # type: ignore[no-untyped-def]
    return make_engine(url, read_only=True)


def _theme() -> charts.Theme:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def _table(frame: pd.DataFrame, empty: str) -> None:
    if frame.empty:
        st.caption(empty)
    else:
        st.dataframe(frame, hide_index=True, width="stretch")


# --------------------------------------------------------------------------- pages


def page_overview(engine, config: AppConfig, status: data.SystemStatus) -> None:  # type: ignore[no-untyped-def]
    view = data.overview(engine, config)
    a, b, c, d = st.columns(4)
    a.metric(
        "Equity",
        _money(view.equity),
        _pct(view.day_pnl_pct) + " today" if view.day_pnl_pct is not None else None,
    )
    b.metric(
        "Since start",
        _pct(view.total_pct),
        f"SPY {_pct(view.spy_total_pct)}" if view.spy_total_pct is not None else None,
        delta_color="off",
        help=f"Since the first recorded snapshot ({view.since or 'none yet'}).",
    )
    c.metric("Open trades", str(view.open_trades))
    d.metric(
        "Closed trades",
        str(view.closed_trades),
        f"win rate {view.win_rate:.0f}%" if view.win_rate is not None else None,
        delta_color="off",
    )
    e, f, g, h = st.columns(4)
    e.metric("Realised P&L", _money(view.realised_usd))
    f.metric(
        "API spend this month",
        _money(view.month_spend),
        f"of {_money(view.budget)}",
        delta_color="off",
    )
    g.metric(
        "App service",
        "running" if status.app_alive else "NOT running",
        "heartbeat never"
        if status.heartbeat_age_seconds is None
        else f"heartbeat {status.heartbeat_age_seconds:.0f}s ago",
        delta_color="off",
    )
    h.metric(
        "Trading",
        "PAUSED" if view.paused is not None else "active",
        "kill switch ON" if view.kill_switch else None,
        delta_color="inverse",
    )
    if view.paused is not None:
        st.warning(f"Trading is paused: {view.paused}. Resume with /resume in Telegram.")
    st.write(
        f"**Last research run:** {view.last_research}  \n**Pattern model in use:** {view.active_model}"
    )
    st.subheader("Open positions")
    _table(data.trades(engine, open_only=True), "No open positions.")
    st.subheader("Today's jobs")
    _table(data.job_runs(engine, days=1), "No jobs have run in the last 24 hours.")


def page_equity(engine) -> None:  # type: ignore[no-untyped-def]
    curve = data.equity_curve(engine)
    if curve.empty:
        st.info(
            "No equity snapshots yet. They are recorded by the monitor and at the end of each trading day."
        )
        return
    st.subheader("Return since start: papertrader vs SPY")
    st.altair_chart(charts.returns_chart(curve, _theme()), width="stretch")
    st.subheader("Drawdown: distance below the best level so far")
    st.altair_chart(charts.drawdown_chart(curve, _theme()), width="stretch")
    st.caption(
        "SPY is the price only (paper trading does not pay dividends either). Before 4-8 weeks of "
        "trading, differences are mostly luck."
    )
    table = curve.assign(day=curve["day"].dt.strftime("%a %d %b %Y")).rename(columns={
        "papertrader_pct": "papertrader %", "spy_pct": "SPY %", "drawdown_pct": "drawdown %"})  # fmt: skip
    st.dataframe(table.iloc[::-1], hide_index=True, width="stretch")


def page_trades(engine) -> None:  # type: ignore[no-untyped-def]
    tests = st.toggle("Show test trades", value=False)
    st.subheader("Open")
    _table(data.trades(engine, open_only=True, include_tests=tests), "No open trades.")
    st.subheader("Closed")
    closed = data.trades(engine, open_only=False, include_tests=tests)
    _table(closed, "No closed trades yet.")
    if not closed.empty:
        st.caption(f"{len(closed)} closed · total {_money(float(closed['P&L $'].sum()))}")


def _evidence(items: list[dict[str, object]]) -> str:
    lines = []
    for item in items:
        url = item.get("url")
        source = f"[{item.get('source_id')}]({url})" if url else f"`{item.get('source_id')}`"
        lines.append(f"- {item.get('claim')} ({source})")
    return "\n".join(lines) or "- none"


def page_decisions(engine) -> None:  # type: ignore[no-untyped-def]
    choices = data.decision_choices(engine)
    if not choices:
        st.info("No decisions yet. The judge decides at 08:20 New York time on trading days.")
        return
    labels = {label: decision_id for decision_id, label in choices}
    picked = st.selectbox("Decision", list(labels))
    view = data.decision_view(engine, labels[picked])
    if view is None:
        st.error("That decision no longer exists.")
        return
    st.caption(f"{view.as_of} New York · {view.kind} · judge {view.model} · run cost {_money(view.cost_usd)}"
               + (f" · trade bar {view.threshold:.0%}" if view.threshold else ""))  # fmt: skip
    st.markdown(f"**Market:** {view.summary}")
    st.subheader("Ideas")
    _table(view.ideas, "No trade ideas: nothing was good enough.")
    left, right = st.columns(2)
    with left:
        st.subheader("Rejected by the judge")
        _table(view.rejected, "None.")
    with right:
        st.subheader("Positions to sell")
        _table(view.exits, "None.")
    st.subheader(f"Agent reports ({len(view.reports)})")
    tickers = sorted({r["ticker"] for r in view.reports})
    chosen = st.multiselect("Only these stocks", tickers)
    for report in view.reports:
        if chosen and report["ticker"] not in chosen:
            continue
        title = (f"{report['ticker']} · {report['direction'].upper()} · {report['agent']} · "
                 f"confidence {report['confidence']:.2f} · {report['days']} days")  # fmt: skip
        if report["vs SPY so far"] is not None:
            title += f" · result vs SPY {report['vs SPY so far']:+.2f}%"
        with st.expander(title):
            st.markdown(f"**Thesis:** {report['thesis']}")
            st.markdown("**Evidence:**\n" + _evidence(report["evidence"]))
            if report["risks"]:
                st.markdown("**Risks:** " + "; ".join(report["risks"]))
            st.caption(
                f"event: {report['event'] or '-'} · claims removed for no source: {report['dropped claims']}"
            )
    st.subheader("Who ran")
    _table(view.runs, "No run details stored.")


def page_leaderboard(engine, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    windows = sorted(set(config.learning.stats_windows_days))
    window = st.radio("Calls made in the last", windows, index=windows.index(config.learning.weight_window_days)
                      if config.learning.weight_window_days in windows else 0, horizontal=True,
                      format_func=lambda d: f"{d} days")  # fmt: skip
    board = data.leaderboard(engine, window)
    if board.as_of is None:
        st.info(
            "No agent call has been scored yet. A call is scored after its holding period ends (1-10 trading days)."
        )
    else:
        st.caption(f"Scored up to {board.as_of:%a %d %b %Y}. Hit rate: share of long/short calls that beat "
                   "(or, for shorts, lagged) SPY. Brier: 0 is perfect, 0.25 is a coin flip.")  # fmt: skip
        _table(board.overall, "No scored calls in this window.")
        left, right = st.columns(2)
        with left:
            st.subheader("By sector")
            _table(board.by_sector, "None yet.")
        with right:
            st.subheader("By event type")
            _table(board.by_event, "None yet.")
    st.subheader("Reliability weights the judge uses")
    history = data.weights(engine)
    if history.empty:
        st.caption("All 1.0 until the first weekly update with enough scored calls.")
    else:
        st.altair_chart(charts.weights_chart(history, _theme()), width="stretch")
        st.dataframe(history.sort_values("week").groupby("agent").tail(1), hide_index=True)
    st.subheader("Latest lessons")
    lesson = data.latest_lesson(engine)
    if lesson is None:
        st.caption("None yet: lessons need at least "
                   f"{config.learning.lessons_min_outcomes} scored calls (written on Saturdays).")  # fmt: skip
    else:
        st.caption(lesson[0])
        st.text(lesson[1])


def page_model(engine) -> None:  # type: ignore[no-untyped-def]
    versions = data.model_versions(engine)
    if versions.empty:
        st.info(
            "No pattern model trained yet. Run: docker compose run --rm app papertrader model train"
        )
        return
    st.caption(
        "Out-of-sample = data the model never saw while training. Daily IC near 0 means no ranking skill. "
        'A model is only used ("active") if it beats the current one on the latest unseen window. '
        "Backtests are optimistic; the forward paper run is the real test."
    )
    st.dataframe(versions, hide_index=True, width="stretch")
    st.caption(
        "Full reports: the reports folder next to docker-compose.yml, or `papertrader model report`."
    )


def page_spend(engine, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    spend = data.api_spend(engine)
    status = data.budget_status(engine, config)
    a, b, c = st.columns(3)
    a.metric(
        "This month",
        _money(spend.month_total),
        f"of {_money(config.budget.monthly_usd)}",
        delta_color="off",
    )
    b.metric(
        "Today",
        _money(spend.today_total),
        f"daily allowance {_money(status.day_budget)}",
        delta_color="off",
    )
    c.metric(
        "Budget mode",
        status.mode,
        status.reason if status.mode != "normal" else None,
        delta_color="off",
    )
    if spend.daily.empty:
        st.info("No Claude API calls in the last 30 days.")
        return
    st.subheader("Per day, last 30 days")
    st.altair_chart(charts.spend_chart(spend.daily, _theme()), width="stretch")
    with st.expander("The same as a table ($ per day)"):
        st.dataframe(spend.daily.sort_index(ascending=False), width="stretch")
    left, right = st.columns(2)
    with left:
        st.subheader("By purpose")
        st.dataframe(spend.by_purpose, hide_index=True, width="stretch")
    with right:
        st.subheader("By model")
        st.dataframe(spend.by_model, hide_index=True, width="stretch")


def page_jobs(engine) -> None:  # type: ignore[no-untyped-def]
    monitor = st.toggle("Include the 10-minute monitor runs", value=False)
    st.subheader("Job runs, last 3 days")
    _table(data.job_runs(engine, days=3, include_monitor=monitor), "No job runs yet.")
    st.subheader("Telegram messages, newest first")
    _table(data.notifications(engine), "No messages yet.")


def page_experiment(config: AppConfig) -> None:
    from papertrader.db.types import utcnow
    from papertrader.experiment.measure import as_rows, compare, get_start
    from papertrader.hub import open_views

    st.subheader("The three-way experiment")
    st.caption("Measured as written in advance in docs/PREREGISTRATION.md. Paper money only.")
    views = open_views(get_settings())
    try:
        engines: dict[str, Any] = {name: view.engine for name, view in views.items()}
        comparison = compare(engines, config, get_start(engines["A"]), utcnow())
    finally:
        for view in views.values():
            view.engine.dispose()
    for line in comparison.verdict:
        st.write(line)
    if comparison.start is None:
        return
    curves = pd.DataFrame(comparison.curves())
    if not curves.empty:
        st.altair_chart(charts.experiment_chart(curves, _theme()), width="stretch")
    table = pd.DataFrame(as_rows(comparison))
    for column in ("Net return", "Gross return", "Hit rate (5 days)"):
        table[column] = table[column].map(lambda v: _pct(v, 1))
    _table(table, "No system has data yet.")
    for test in comparison.tests:
        if test.mean is not None:
            band = (
                f"95% interval {test.interval[0]:+.4f} to {test.interval[1]:+.4f}"
                if test.interval
                else "too few days for an interval"
            )
            st.write(f"**{test.name}** {test.question}: mean {test.mean:+.4f} over {test.days} days; {band}"
                     + (f"; p {test.p_holm:.2f} after Holm" if test.p_holm is not None else ""))  # fmt: skip


def page_settings(config: AppConfig, status: data.SystemStatus) -> None:
    st.table({
        "Setting": ["Universe", "Research agents", "Judge", "Trade threshold", "Max per position",
                    "Max positions", "Max per sector", "Shorts", "Daily loss kill switch", "Monthly API budget"],
        "Value": [
            config.universe.index, f"{config.agents.defaults.model} (effort {config.agents.defaults.effort})",
            f"{config.judge.model} (effort {config.judge.effort})", f"{config.judge.confidence_threshold:.0%} confidence",
            f"{config.risk.max_position_pct:.0%} of equity", str(config.risk.max_open_positions),
            f"{config.risk.max_sector_pct:.0%} of equity", "allowed" if config.risk.allow_shorts else "disabled",
            f"-{config.risk.daily_loss_kill_switch_pct:.0%}", f"${config.budget.monthly_usd:,.0f}",
        ],
    })  # fmt: skip
    st.subheader("Database tables")
    st.dataframe({"Table": list(status.table_rows), "Rows": list(status.table_rows.values())},
                 hide_index=True, width="stretch")  # fmt: skip


# --------------------------------------------------------------------------- frame

st.title("papertrader")
st.caption(
    f"Version {__version__} - PAPER TRADING ONLY ({ALPACA_PAPER_BASE_URL}). "
    "An experiment, not advice. Times are New York time."
)

try:
    config = get_config()
except ConfigError as exc:
    st.error(f"config.yaml problem: {exc}")
    st.stop()

try:
    engine = _engine(get_settings().database_url)
    status = data.load_status(engine)
except Exception as exc:
    st.error(f"Cannot reach the database: {exc}")
    st.stop()

if not status.schema_ready:
    st.warning(
        "The database has no tables yet. Start the app service (it creates them) and reload."
    )
    st.stop()

page = st.sidebar.radio("Page", PAGES)
st.sidebar.caption("Read-only. Nothing on these pages can change the system.")
if page == "Overview":
    page_overview(engine, config, status)
elif page == "Experiment: A vs B vs C":
    page_experiment(config)
elif page == "Equity vs SPY":
    page_equity(engine)
elif page == "Trades":
    page_trades(engine)
elif page == "Decisions and reports":
    page_decisions(engine)
elif page == "Agent leaderboard":
    page_leaderboard(engine, config)
elif page == "Pattern model":
    page_model(engine)
elif page == "API spend":
    page_spend(engine, config)
elif page == "Jobs and messages":
    page_jobs(engine)
else:
    page_settings(config, status)
