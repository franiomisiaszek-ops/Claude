"""Command line entry point: `papertrader <command>`."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer

from papertrader import __version__
from papertrader.config import ConfigError
from papertrader.safety import ALPACA_PAPER_BASE_URL, LiveTradingForbiddenError

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Multi-agent research and PAPER trading experiment. Never trades real money.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(no_args_is_help=True, help="Fetch market data, news, filings and macro.")
app.add_typer(ingest_app, name="ingest")
model_app = typer.Typer(no_args_is_help=True, help="The pattern model: train, report, status.")
app.add_typer(model_app, name="model")
trade_app = typer.Typer(
    no_args_is_help=True, help="Paper trading on Alpaca: orders, monitoring, exits."
)
app.add_typer(trade_app, name="trade")
telegram_app = typer.Typer(no_args_is_help=True, help="Telegram messages and the bot.")
app.add_typer(telegram_app, name="telegram")
jobs_app = typer.Typer(no_args_is_help=True, help="The daily schedule: see it, run a job by hand.")
app.add_typer(jobs_app, name="jobs")


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def check() -> None:
    """Validate config.yaml and .env, the paper-only guard and the database connection."""
    from papertrader.runtime import bootstrap, migration_status

    try:
        runtime = bootstrap()
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc

    typer.echo(f"Trading endpoint : {ALPACA_PAPER_BASE_URL}  (PAPER only)")
    typer.echo(f"Universe         : {runtime.config.universe.index}")
    typer.echo(f"Monthly budget   : ${runtime.config.budget.monthly_usd:.2f}")
    typer.echo(
        f"Judge model      : {runtime.config.judge.model} (effort {runtime.config.judge.effort})"
    )
    defaults = runtime.config.agents.defaults
    typer.echo(f"Research agents  : {defaults.model} (effort {defaults.effort})")
    typer.echo(f"Database         : {runtime.engine.url.render_as_string(hide_password=True)}")

    current, head = migration_status(runtime)
    if current == head:
        typer.echo(f"Schema           : up to date ({head})")
    else:
        where = "not created yet" if current is None else f"at {current}"
        typer.echo(f"Schema           : {where} (code expects {head}); run: papertrader db upgrade")

    typer.echo("Keys in .env     :")
    for name, present in runtime.settings.secret_status().items():
        typer.echo(f"  {'set    ' if present else 'MISSING'}  {name}")
    typer.secho("OK", fg=typer.colors.GREEN)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create or update the database tables to the latest schema."""
    from papertrader.config import get_settings
    from papertrader.db.migrate import head_revision, upgrade_to_head

    url = get_settings().database_url
    upgrade_to_head(url)
    typer.echo(f"Database schema is at revision {head_revision(url)}.")


@app.command()
def run() -> None:
    """Start the long-running service (scheduler). This is what the Docker app container runs."""
    from papertrader.runtime import bootstrap
    from papertrader.scheduler.service import run_forever

    try:
        runtime = bootstrap(migrate=True)
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    run_forever(runtime)


@app.command("dry-run")
def dry_run(
    date: Annotated[
        str | None,
        typer.Option(help="Decide as of this date (YYYY-MM-DD), at the judge's scheduled time."),
    ] = None,
    at: Annotated[
        str | None, typer.Option(help="New York time of day (HH:MM) to decide at, with --date.")
    ] = None,
    max_cost: Annotated[
        float, typer.Option(help="Stop making API calls once this run has spent this many dollars.")
    ] = 5.0,
    agents: Annotated[
        str | None, typer.Option(help="Only these agents, comma-separated, e.g. macro,technicals.")
    ] = None,
    estimate_only: Annotated[
        bool, typer.Option("--estimate-only", help="Show what a run would cost; make no API calls.")
    ] = False,
) -> None:
    """Run the research agents and the judge once and print the full decision.

    Nothing is traded. Only data that was available at the chosen moment is used.
    """
    from datetime import date as date_type
    from datetime import time as time_type

    from anthropic import AsyncAnthropic

    from papertrader.config import KNOWN_AGENTS
    from papertrader.db.models import CycleKind
    from papertrader.db.types import utcnow
    from papertrader.ingest.context import open_context
    from papertrader.ingest.timeutil import at_new_york
    from papertrader.llm.client import ClaudeClient, make_anthropic
    from papertrader.llm.governor import CostGovernor
    from papertrader.research.cycle import CycleResult, run_cycle
    from papertrader.research.report import render
    from papertrader.runtime import bootstrap

    only = _split(agents)
    if only and (unknown := sorted(set(only) - KNOWN_AGENTS)):
        raise _fail(
            f"unknown agent(s) {', '.join(unknown)}. Agents: {', '.join(sorted(KNOWN_AGENTS))}"
        )
    try:
        runtime = bootstrap(migrate=True, log_format="console")
        if date is None:
            as_of = utcnow()
        else:
            clock = time_type.fromisoformat(at) if at else runtime.config.schedule.score_and_judge
            as_of = at_new_york(date_type.fromisoformat(date), clock)
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    except ValueError as exc:
        raise _fail(f"could not read the date or time: {exc}") from exc
    if as_of > utcnow():
        raise _fail("that moment is in the future")

    key = runtime.settings.anthropic_api_key
    if key is None and not estimate_only:
        raise _fail(
            "ANTHROPIC_API_KEY is not set in .env. Add it, or use --estimate-only to see "
            "what a run would cost without calling Claude."
        )

    async def go() -> CycleResult:
        # With --estimate-only no request is ever sent, so a placeholder key is fine.
        api = make_anthropic(key.get_secret_value()) if key else AsyncAnthropic(api_key="unused")
        governor = CostGovernor(
            runtime.session_factory, runtime.config.budget, cycle_cap_usd=max_cost
        )
        claude = ClaudeClient(api, runtime.session_factory, runtime.config.budget, governor)
        async with open_context(runtime.config, runtime.settings, runtime.session_factory) as ctx:
            return await run_cycle(
                config=runtime.config, sessions=runtime.session_factory, claude=claude,
                as_of=as_of, kind=CycleKind.DRY_RUN, only=only, sec=ctx.sec,
                estimate_only=estimate_only,
            )  # fmt: skip

    result = asyncio.run(go())
    typer.echo("")
    typer.echo(render(result))
    if result.status == "failed":
        raise typer.Exit(code=1)


@model_app.command("train")
def model_train() -> None:
    """Build the dataset, validate walk-forward, backtest, and write the report.

    The new model only becomes active if it beats the current one on the latest unseen
    window. Makes no API calls and places no trades.
    """
    import shutil

    from papertrader.db.types import utcnow
    from papertrader.model.pipeline import NotEnoughDataError, run_training
    from papertrader.model.report import sanity_warnings
    from papertrader.runtime import bootstrap

    try:
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    try:
        result = run_training(runtime.session_factory, runtime.config, as_of=utcnow(),
                              data_dir=runtime.settings.data_dir)  # fmt: skip
    except NotEnoughDataError as exc:
        raise _fail(str(exc)) from exc

    oos, bt = result.report["oos"], result.report["backtest"]
    typer.echo(f"\nModel {result.version}: {result.report['promotion']}")
    typer.echo(
        f"Rows: {result.report['data']['rows']:,} | out-of-sample rows: {oos.get('rows', 0):,}"
    )
    typer.echo(f"Hit rate {oos.get('hit_rate', 0):.1%} (base rate {oos.get('base_rate', 0):.1%}) | "
               f"daily IC {oos.get('ic_daily')} | Brier {oos.get('brier', 0):.4f} "
               f"(naive {oos.get('brier_naive', 0):.4f})")  # fmt: skip
    if bt:
        typer.echo(f"Backtest: strategy {bt['strategy'].get('total_return', 0):+.1%} vs SPY "
                   f"{bt['benchmark'].get('total_return', 0):+.1%} vs random picks "
                   f"{bt['random']['total_return_mean']:+.1%}; beat "
                   f"{bt['random']['strategy_percentile']:.0%} of random selections")  # fmt: skip
    for warning in sanity_warnings(result.report):
        typer.secho("! " + warning.replace("**", ""), fg=typer.colors.YELLOW)

    target = runtime.settings.reports_dir
    try:  # a copy where it is easy to open; the originals stay with the model files
        target.mkdir(parents=True, exist_ok=True)
        for path in (result.markdown_path, result.html_path):
            shutil.copy(path, target / f"{result.version}_{path.name}")
        typer.echo(f"\nReport: {target / (result.version + '_backtest_report.html')}")
    except OSError:
        typer.echo(f"\nReport: {result.html_path}")
    typer.echo("Print it here with: papertrader model report")


def _text_client() -> tuple[Any, Any, Any]:
    """Runtime, Claude client and governor for the text-feature commands."""
    from papertrader.llm import client as llm_client
    from papertrader.llm.governor import CostGovernor
    from papertrader.runtime import bootstrap

    try:
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    key = runtime.settings.anthropic_api_key
    if key is None:
        raise _fail("ANTHROPIC_API_KEY is not set in .env.")
    governor = CostGovernor(runtime.session_factory, runtime.config.budget)
    return runtime, llm_client.make_anthropic(key.get_secret_value()), governor


@model_app.command("text-submit")
def model_text_submit(
    limit: Annotated[int, typer.Option(help="At most this many headlines in this batch.")] = 20000,
) -> None:
    """Send masked historical headlines to Claude for labelling (Batches API, half price).

    Results are not immediate: run `papertrader model text-collect` later (usually
    within an hour) to store them. Optional: the model trains without these features.
    """
    from papertrader.llm.client import BudgetExceededError
    from papertrader.model.llm_text import submit

    runtime, api, governor = _text_client()
    try:
        batch_id, items, estimate = asyncio.run(
            submit(api, runtime.session_factory, runtime.config, governor, limit=limit)
        )
    except BudgetExceededError as exc:
        raise _fail(f"Not sent: {exc}") from exc
    if batch_id is None:
        typer.echo("Nothing to send: every eligible headline already has features or is waiting.")
        return
    typer.echo(f"Sent {items:,} masked headlines as batch {batch_id}.")
    typer.echo(f"Estimated cost: at most about ${estimate:.2f} (billed at half price).")
    typer.echo("Collect the results later with: papertrader model text-collect")


@model_app.command("text-collect")
def model_text_collect() -> None:
    """Store the results of finished text batches and record their cost."""
    from papertrader.model.llm_text import collect

    runtime, api, _governor = _text_client()
    summary = asyncio.run(collect(api, runtime.session_factory, runtime.config))
    typer.echo(
        f"Stored features for {summary['stored']:,} headlines; cost ${summary['cost_usd']:.3f}."
    )
    if summary["failed_requests"]:
        typer.echo(f"{summary['failed_requests']} request(s) gave no usable answer; "
                   "those headlines will be sent again next time.")  # fmt: skip
    if summary["still_running"]:
        typer.echo(
            f"Still being processed: {', '.join(summary['still_running'])}. Try again later."
        )


@model_app.command("report")
def model_report() -> None:
    """Print the latest backtest report (Markdown)."""
    from papertrader.config import get_settings

    reports = sorted((get_settings().data_dir / "model").glob("pm-*/backtest_report.md"))
    if not reports:
        raise _fail("No report yet. Run `papertrader model train` first.")
    typer.echo(reports[-1].read_text(encoding="utf-8"))


@model_app.command("status")
def model_status() -> None:
    """List trained models and show which one is active."""
    from sqlalchemy import select

    from papertrader.db.models import ModelVersion
    from papertrader.runtime import bootstrap

    runtime = bootstrap(migrate=True, log_format="console")
    with runtime.session_factory() as session:
        versions = session.scalars(select(ModelVersion).order_by(ModelVersion.id)).all()
        if not versions:
            typer.echo("No model has been trained yet.")
        for row in versions:
            ic = (row.metrics or {}).get("ic_daily")
            typer.echo(f"{row.version}  {'ACTIVE' if row.is_active else 'inactive':<8}  trained to "
                       f"{row.trained_to}  daily IC {ic}  {row.notes or ''}")  # fmt: skip


# ----------------------------------------------------------------------------- trading


def _trading() -> tuple[Any, Any]:
    """Runtime and trading engine, connected to the Alpaca PAPER account."""
    from papertrader.runtime import bootstrap
    from papertrader.trading.broker import AlpacaPaperBroker, BrokerError
    from papertrader.trading.engine import TradingEngine

    try:
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    settings = runtime.settings
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        raise _fail("ALPACA_API_KEY and ALPACA_SECRET_KEY (paper account) are not set in .env.")
    try:
        broker = AlpacaPaperBroker(
            settings.alpaca_api_key.get_secret_value(), settings.alpaca_secret_key.get_secret_value(),
            live_feed=runtime.config.trading.live_feed,
        )  # fmt: skip
    except (LiveTradingForbiddenError, BrokerError) as exc:
        raise _fail(str(exc)) from exc
    return runtime, TradingEngine(runtime.session_factory, broker, runtime.config)


def _latest_notification_id(runtime: Any) -> int:
    from sqlalchemy import func, select

    from papertrader.db.models import Notification

    with runtime.session_factory() as session:
        return int(session.scalar(select(func.max(Notification.id))) or 0)


def _show_notifications(runtime: Any, after: int) -> None:
    """Messages written to the outbox by this command (sent by Telegram from Phase 6)."""
    from sqlalchemy import select

    from papertrader.db.models import Notification

    with runtime.session_factory() as session:
        rows = session.scalars(
            select(Notification).where(Notification.id > after).order_by(Notification.id)
        ).all()
    if rows:
        typer.echo("\nMessages queued for Telegram:")
        for row in rows:
            typer.echo("  " + row.text.replace("\n", "\n  "))


def _placements(results: list[Any]) -> None:
    if not results:
        typer.echo("No approved trade ideas to place.")
    for r in results:
        limit = f" limit {r.limit_price:.2f}" if r.limit_price else ""
        typer.echo(
            f"  {r.ticker:<6} {r.status:<10} {f'{r.qty} shares{limit}' if r.qty else ''}  {r.detail}"
        )


@trade_app.command("status")
def trade_status() -> None:
    """Account, positions, open orders and whether trading is paused."""
    from sqlalchemy import select

    from papertrader.db.models import Trade, TradeStatus

    runtime, engine = _trading()
    account = engine.broker.account()
    clock = engine.broker.clock()
    typer.echo(f"Paper account: equity ${account.equity:,.2f}, cash ${account.cash:,.2f}, "
               f"today {account.day_change_pct:+.2%} ({account.status})")  # fmt: skip
    typer.echo(f"Market: {'OPEN' if clock.is_open else 'closed'}")
    paused = engine.paused()
    typer.echo(f"Trading: {'PAUSED - ' + str(paused.get('reason')) if paused else 'active'}")
    positions = engine.broker.positions()
    typer.echo(f"\nPositions ({len(positions)}):")
    for p in positions:
        typer.echo(f"  {p.symbol:<6} {p.qty:g} @ {p.avg_entry_price:.2f}, now {p.current_price:.2f} "
                   f"({p.current_price / p.avg_entry_price - 1:+.2%}), value ${p.market_value:,.2f}")  # fmt: skip
    orders = engine.broker.open_orders()
    typer.echo(f"\nOpen orders ({len(orders)}):")
    for o in orders:
        price = o.limit_price or o.stop_price
        typer.echo(f"  {o.symbol:<6} {o.side} {o.qty:g} {o.order_type}{f' {price:.2f}' if price else ''} "
                   f"({o.status}, {len(o.legs)} legs)")  # fmt: skip
    with runtime.session_factory() as session:
        trades = session.scalars(select(Trade).where(Trade.status == TradeStatus.OPEN)).all()
        typer.echo(f"\nOpen trades in the database ({len(trades)}):")
        for t in trades:
            ends = (
                f", time stop {t.horizon_ends_at:%Y-%m-%d %H:%M} UTC" if t.horizon_ends_at else ""
            )
            typer.echo(
                f"  {t.ticker:<6} {t.qty:g} @ {t.entry_price:.2f}{' [TEST]' if t.is_test else ''}{ends}"
            )


@trade_app.command("place")
def trade_place(
    decision: Annotated[
        int | None, typer.Option(help="Place this decision's ideas (default: today's).")
    ] = None,
) -> None:
    """Place bracket orders for today's approved trade ideas, within the risk limits."""
    from papertrader.trading.engine import TradingRefused

    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    try:
        _placements(engine.place_orders(decision))
    except TradingRefused as exc:
        raise _fail(f"No orders placed: {exc}.") from exc
    _show_notifications(runtime, start)


@trade_app.command("test-order")
def trade_test_order(
    ticker: Annotated[str, typer.Argument(help="Stock to buy, e.g. AAPL.")],
    qty: Annotated[int, typer.Option(help="Number of shares.")] = 1,
) -> None:
    """Buy a few shares with a bracket order (+2% target, -2% stop, sold at today's close).

    For checking the order flow. Marked as a test: it never counts in the statistics.
    """
    from papertrader.trading.engine import TradingRefused

    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    try:
        decision_id, idea_id = engine.create_test_idea(ticker)
        _placements(engine.place_orders(decision_id, qty_overrides={idea_id: qty}))
    except TradingRefused as exc:
        raise _fail(f"No order placed: {exc}.") from exc
    typer.echo("Check on it with: papertrader trade monitor")
    _show_notifications(runtime, start)


def _thesis_checks(runtime: Any, engine: Any) -> list[str]:
    """Fetch fresh news for held stocks, then review any with bad news (needs Claude)."""
    from papertrader.db.types import utcnow
    from papertrader.llm.client import ClaudeClient, make_anthropic
    from papertrader.llm.governor import CostGovernor
    from papertrader.trading.thesis import check_held_positions

    def claude() -> ClaudeClient | None:
        key = runtime.settings.anthropic_api_key
        if key is None:
            return None
        governor = CostGovernor(runtime.session_factory, runtime.config.budget)
        return ClaudeClient(make_anthropic(key.get_secret_value()), runtime.session_factory,
                            runtime.config.budget, governor)  # fmt: skip

    return asyncio.run(check_held_positions(
        config=runtime.config, settings=runtime.settings, sessions=runtime.session_factory,
        engine=engine, claude=claude, now=utcnow(),
    ))  # fmt: skip


def _monitor_once(runtime: Any, engine: Any, *, thesis: bool) -> None:
    report = engine.monitor()
    line = (f"equity ${report.equity:,.2f} ({report.day_change_pct:+.2%} today) | "
            f"opened {report.sync.opened or '-'} | closed {report.sync.closed or '-'}")  # fmt: skip
    if report.time_stops:
        line += f" | time stops {report.time_stops}"
    if report.kill_switch:
        line += " | KILL SWITCH ACTIVE"
    typer.echo(line)
    for warning in report.warnings:
        typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)
    if thesis:
        for note in _thesis_checks(runtime, engine):
            typer.echo(f"  {note}")


@trade_app.command("monitor")
def trade_monitor(
    loop: Annotated[bool, typer.Option(help="Keep checking until the market closes.")] = False,
    interval: Annotated[
        int | None, typer.Option(help="Minutes between checks (default from config).")
    ] = None,
    thesis: Annotated[
        bool, typer.Option(help="Review held stocks that get bad news (uses Claude).")
    ] = True,
) -> None:
    """Check fills, time stops, the kill switch and bad news on held stocks."""
    import time

    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    minutes = interval or runtime.config.schedule.monitor_interval_minutes
    while True:
        _monitor_once(runtime, engine, thesis=thesis)
        if not loop or not engine.broker.clock().is_open:
            break
        time.sleep(minutes * 60)
    _show_notifications(runtime, start)


@trade_app.command("close")
def trade_close(
    ticker: Annotated[str, typer.Argument(help="Stock to sell, e.g. AAPL.")],
    reason: Annotated[str, typer.Option(help="Why (recorded with the trade).")] = "closed by hand",
) -> None:
    """Sell an open position now with a market order (its stop and target are cancelled)."""
    import time

    from papertrader.db.models import ExitTrigger

    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    typer.echo(engine.exit_ticker(ticker, ExitTrigger.MANUAL, reason))
    time.sleep(3)
    report = engine.sync()
    if report.closed:
        typer.echo(f"Closed: {', '.join(report.closed)}")
    elif not engine.broker.clock().is_open:
        typer.echo("The market is closed: the sale will happen at the next open.")
    _show_notifications(runtime, start)


@trade_app.command("reconcile")
def trade_reconcile() -> None:
    """End of day: cancel entries that did not fill, record equity, write the summary."""
    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    summary = engine.end_of_day()
    if summary["expired"]:
        typer.echo(f"Unfilled entries cancelled: {', '.join(summary['expired'])}")
    _show_notifications(runtime, start)


@trade_app.command("pause")
def trade_pause(reason: Annotated[str, typer.Option(help="Why.")] = "paused by hand") -> None:
    """Stop placing new orders. Open positions keep their stops and targets."""
    _runtime, engine = _trading()
    engine.pause(reason)
    typer.echo("Trading paused. Resume with: papertrader trade resume")


@trade_app.command("resume")
def trade_resume() -> None:
    """Allow new orders again (also clears the kill switch)."""
    _runtime, engine = _trading()
    engine.resume()
    typer.echo("Trading resumed.")


@trade_app.command("day")
def trade_day(
    test_ticker: Annotated[
        str | None, typer.Option(help="Also buy one test share of this stock.")
    ] = "SPY",
    research: Annotated[
        bool, typer.Option(help="Run the agents and judge first (costs about $1-2).")
    ] = False,
    minutes: Annotated[int, typer.Option(help="How long to keep monitoring.")] = 20,
    interval: Annotated[int, typer.Option(help="Minutes between checks.")] = 2,
) -> None:
    """A full trading day, compressed: (research), orders, monitoring, exits, end of day.

    Run it while the US market is open (15:35-22:00 Polish time on most days). The test
    share is sold again at the end, so the account is left as it was apart from costs.
    """
    import time

    from papertrader.db.models import CycleKind, ExitTrigger
    from papertrader.db.types import utcnow
    from papertrader.trading.engine import TradingRefused

    runtime, engine = _trading()
    start = _latest_notification_id(runtime)
    if not engine.broker.clock().is_open:
        raise _fail("The market is closed. Run this between 09:35 and 16:00 New York time.")

    if research:
        from papertrader.ingest.context import open_context
        from papertrader.llm.client import ClaudeClient, make_anthropic
        from papertrader.llm.governor import CostGovernor
        from papertrader.research.cycle import run_cycle
        from papertrader.research.report import render

        key = runtime.settings.anthropic_api_key
        if key is None:
            raise _fail("--research needs ANTHROPIC_API_KEY in .env.")

        async def research_now() -> Any:
            governor = CostGovernor(
                runtime.session_factory, runtime.config.budget, cycle_cap_usd=3.0
            )
            claude = ClaudeClient(make_anthropic(key.get_secret_value()), runtime.session_factory,
                                  runtime.config.budget, governor)  # fmt: skip
            async with open_context(
                runtime.config, runtime.settings, runtime.session_factory
            ) as ctx:
                return await run_cycle(config=runtime.config, sessions=runtime.session_factory, claude=claude,
                                       as_of=utcnow(), kind=CycleKind.DAILY, sec=ctx.sec)  # fmt: skip

        typer.echo("1. Research (agents and judge)...")
        typer.echo(render(asyncio.run(research_now())))
    else:
        typer.echo("1. Research skipped (add --research to run the agents and judge).")

    typer.echo("\n2. Orders")
    test_trade_ticker = None
    try:
        _placements(engine.place_orders())
        if test_ticker:
            decision_id, idea_id = engine.create_test_idea(test_ticker)
            results = engine.place_orders(decision_id, qty_overrides={idea_id: 1})
            _placements(results)
            if any(r.status == "submitted" for r in results):
                test_trade_ticker = test_ticker.upper()
    except TradingRefused as exc:
        raise _fail(f"No orders placed: {exc}.") from exc

    typer.echo(f"\n3. Monitoring every {interval} min for {minutes} min")
    deadline = time.monotonic() + minutes * 60
    while True:
        _monitor_once(runtime, engine, thesis=True)
        if time.monotonic() >= deadline or not engine.broker.clock().is_open:
            break
        time.sleep(interval * 60)

    if test_trade_ticker:
        typer.echo(f"\n4. Selling the test share of {test_trade_ticker}")
        typer.echo(
            "  "
            + engine.exit_ticker(test_trade_ticker, ExitTrigger.MANUAL, "end of the simulated day")
        )
        time.sleep(5)
        engine.sync()

    typer.echo("\n5. End of day")
    summary = engine.end_of_day()
    if summary["expired"]:
        typer.echo(f"  unfilled entries cancelled: {', '.join(summary['expired'])}")
    _show_notifications(runtime, start)


# ----------------------------------------------------------------------------- telegram


def _telegram(need_chat: bool = True) -> tuple[Any, Any]:
    from papertrader.notify.service import make_client
    from papertrader.runtime import bootstrap

    try:
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    if runtime.settings.telegram_bot_token is None:
        raise _fail("TELEGRAM_BOT_TOKEN is not set in .env. See README, section Telegram.")
    if need_chat and not runtime.settings.telegram_chat_id:
        raise _fail(
            "TELEGRAM_CHAT_ID is not set in .env. Run `papertrader telegram setup` to find it."
        )
    return runtime, make_client(runtime.settings)


def _flush(runtime: Any, client: Any) -> None:
    from papertrader.notify.telegram import flush_outbox

    report = flush_outbox(runtime.session_factory, client, str(runtime.settings.telegram_chat_id),
                          runtime.config.telegram)  # fmt: skip
    typer.echo(f"Sent {report.sent}, failed {report.failed}, dropped as too old {report.dropped}"
               + (" (stopped early; the rest goes next time)" if report.waiting else "") + ".")  # fmt: skip
    if report.setup_problem:
        raise _fail(f"{report.setup_problem}. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env "
                    "(run `papertrader telegram setup`).")  # fmt: skip


@telegram_app.command("setup")
def telegram_setup() -> None:
    """Check the bot token and show the chat id to put in TELEGRAM_CHAT_ID."""
    from papertrader.notify.telegram import TelegramError

    _runtime, client = _telegram(need_chat=False)
    try:
        me = client.me()
        updates = client.updates(None, 0)
    except TelegramError as exc:
        raise _fail(f"{exc}. Check TELEGRAM_BOT_TOKEN.") from exc
    typer.echo(f"Bot: @{me.get('username')} ({me.get('first_name')}) - the token works.")
    chats = {}
    for update in updates:
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        typer.echo(f"No messages yet. In Telegram, open @{me.get('username')}, press Start or send 'hi', "
                   "then run this command again.")  # fmt: skip
        return
    for chat_id, name in chats.items():
        typer.echo(f"Chat {name}: TELEGRAM_CHAT_ID={chat_id}")
    typer.echo("Put that line in .env, then restart the app: docker compose up -d")


@telegram_app.command("test")
def telegram_test() -> None:
    """Send one sample message of every type, marked [TEST]."""
    from datetime import timedelta

    from papertrader.db.types import utcnow
    from papertrader.ingest.timeutil import new_york_date
    from papertrader.notify import messages
    from papertrader.notify.reports import BriefTrade, format_brief, format_weekly
    from papertrader.notify.telegram import TelegramError

    runtime, client = _telegram()
    now = utcnow()
    samples = {
        "Morning brief": format_brief(new_york_date(now), "Sample: rates steady, tech leading, earnings season quiet.", [
            BriefTrade(1, "buy", "NVDA", 0.82, 141.0, 142.5, 150.6, 137.8, "Strong data-centre demand. More text.", "proposed"),
            BriefTrade(2, "buy", "JPM", 0.61, 149.0, 150.5, 159.0, 145.5, "Loan growth picking up.", "below_threshold"),
        ], ["AAPL +1.2%"]),
        "Trade executed": messages.trade_executed(side="buy", qty=20, ticker="NVDA", price=142.10, confidence=0.82,
                                                  reason="Strong data-centre demand"),
        "Sell signal": messages.sell_signal(ticker="NVDA", trigger="target", reason="take-profit at 150.60 filled"),
        "Trade closed": messages.trade_closed(ticker="NVDA", exit_price=150.60, pnl_usd=170.0, pnl_pct=0.0598,
                                              excess_pct=0.049, opened=now - timedelta(days=3), closed=now,
                                              trigger="target", right=["earnings", "technicals"], wrong=["contrarian"]),
        "End of day": messages.eod_summary(day=now, equity=100_420.0, day_pnl=320.0, day_pnl_pct=0.0032,
                                           total_pct=0.0042, spy_total_pct=0.0031, win_rate=0.6, closed_trades=5,
                                           positions=[("AAPL", 9_800.0, 0.012)]),
        "Weekly": format_weekly(end=new_york_date(now), closed=5, wins=3, pnl=640.0, excess=0.004,
                                leaderboard=[("earnings", 12, 0.58), ("technicals", 20, 0.51), ("contrarian", 9, 0.44)],
                                model_line="pm-20261116 not promoted, daily IC -0.013", spend_week=6.40,
                                spend_parts={"agents": 4.10, "judge": 2.30}, spend_month=21.70, budget=100.0),
        "Alert": messages.alert("KILL SWITCH: account down -3.1% today. Trading paused (sample)."),
    }  # fmt: skip
    failures = 0
    for name, text in samples.items():
        try:
            client.send(str(runtime.settings.telegram_chat_id), "[TEST] " + text)
            typer.echo(f"  sent: {name}")
        except TelegramError as exc:
            failures += 1
            typer.secho(f"  FAILED: {name}: {exc}", fg=typer.colors.RED)
    if failures:
        raise typer.Exit(code=1)
    typer.echo("All 7 message types sent. Now try /status and /help in the chat with your bot "
               "(the app container must be running: docker compose up -d).")  # fmt: skip


@telegram_app.command("send")
def telegram_send() -> None:
    """Send the messages waiting in the outbox now."""
    runtime, client = _telegram()
    _flush(runtime, client)


@telegram_app.command("brief")
def telegram_brief() -> None:
    """Send today's morning brief now (from today's daily research run)."""
    from papertrader.db.types import utcnow
    from papertrader.notify.outbox import queue
    from papertrader.notify.reports import build_brief

    runtime, client = _telegram()
    with runtime.session_factory() as session:
        text = build_brief(session, runtime.config, utcnow())
        if text is None:
            raise _fail("No daily research run today, so there is no brief to send.")
        queue(session, "morning_brief", text)
        session.commit()
    _flush(runtime, client)


@telegram_app.command("weekly")
def telegram_weekly() -> None:
    """Send the weekly summary now."""
    from papertrader.db.types import utcnow
    from papertrader.notify.outbox import queue
    from papertrader.notify.reports import build_weekly

    runtime, client = _telegram()
    with runtime.session_factory() as session:
        queue(session, "weekly", build_weekly(session, runtime.config, utcnow()))
        session.commit()
    _flush(runtime, client)


@telegram_app.command("listen")
def telegram_listen() -> None:
    """Answer bot commands and send messages in this window, until Ctrl+C.

    Not needed when the app container is running: it does this all the time.
    """
    from papertrader.notify.bot import CommandBot
    from papertrader.notify.service import make_broker_factory
    from papertrader.notify.telegram import TelegramError

    runtime, client = _telegram()
    bot = CommandBot(runtime.session_factory, runtime.config, client, str(runtime.settings.telegram_chat_id),
                     broker=make_broker_factory(runtime.settings, runtime.config))  # fmt: skip
    typer.echo("Listening. Send /help to your bot. Ctrl+C to stop.")
    try:
        while True:
            try:
                answered = bot.poll_once()
                if answered:
                    typer.echo(f"answered {answered} command(s)")
                _flush(runtime, client)
            except TelegramError as exc:
                typer.echo(f"Telegram problem: {exc}; retrying")
    except KeyboardInterrupt:
        typer.echo("Stopped.")


# ----------------------------------------------------------------------------- jobs


def _jobs() -> Any:
    from papertrader.runtime import bootstrap
    from papertrader.scheduler.jobs import Jobs

    try:
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc
    return Jobs(runtime)


@jobs_app.command("list")
def jobs_list() -> None:
    """The schedule (New York time) and how each job went today."""
    from sqlalchemy import select

    from papertrader.db.models import JobRun
    from papertrader.db.types import utcnow
    from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
    from papertrader.scheduler.jobs import SPECS

    jobs = _jobs()
    now = utcnow()
    day = new_york_date(now)
    cfg = jobs.config.schedule
    trading = jobs.trading_day(day)
    if trading is None:
        typer.echo(f"Today ({day:%a %d %b}) the market is closed.")
    else:
        typer.echo(f"Today ({day:%a %d %b}) the market is open "
                   f"{trading.open.astimezone(NEW_YORK):%H:%M}-{trading.close.astimezone(NEW_YORK):%H:%M} New York time.")  # fmt: skip
    from datetime import time as time_type
    from datetime import timedelta

    start, end = at_new_york(day, time_type(0)), at_new_york(day + timedelta(days=1), time_type(0))
    with jobs.sessions() as session:
        runs = session.scalars(select(JobRun).where(JobRun.scheduled_for >= start, JobRun.scheduled_for < end)
                               .order_by(JobRun.id)).all()  # fmt: skip
    latest = {run.job_name: run for run in runs}
    typer.echo(f"\n{'job':<18}{'when':<16}{'today':<11}what it does")
    for spec in SPECS:
        if spec.at is None:
            when = f"every {cfg.monitor_interval_minutes} min"
        elif spec.weekly:
            when = f"{cfg.weekly_day} {spec.at(jobs.config):%H:%M}"
        else:
            when = f"{spec.at(jobs.config):%H:%M}"
        run = latest.get(spec.name)
        state = run.status.value if run else "-"
        typer.echo(f"{spec.name:<18}{when:<16}{state:<11}{spec.title}")
    typer.echo("\nRun one now with: papertrader jobs run NAME")


@jobs_app.command("run")
def jobs_run(
    name: Annotated[str, typer.Argument(help="Job name, from `papertrader jobs list`.")],
) -> None:
    """Run one job now (also on a weekend or holiday). Recorded like a scheduled run."""
    from papertrader.scheduler.jobs import BY_NAME

    if name not in BY_NAME:
        raise _fail(f"unknown job {name}. Jobs: {', '.join(BY_NAME)}")
    jobs = _jobs()
    start = _latest_notification_id(jobs.runtime)
    outcome = jobs.run(name, manual=True)
    colour = {"succeeded": typer.colors.GREEN, "failed": typer.colors.RED}.get(outcome.status)
    typer.secho(
        f"{name}: {outcome.status}" + (f" - {outcome.detail}" if outcome.detail else ""), fg=colour
    )
    for key in ("placements", "exits", "warnings", "notes", "weights"):
        for line in outcome.details.get(key) or []:
            typer.echo(f"  {line}")
    _show_notifications(jobs.runtime, start)
    if outcome.status == "failed":
        raise typer.Exit(code=1)


@jobs_app.command("history")
def jobs_history(
    days: Annotated[int, typer.Option(help="How many days back.")] = 3,
    monitor: Annotated[bool, typer.Option(help="Include the 10-minute monitor runs.")] = False,
) -> None:
    """Recent job runs: when, how long, result."""
    from datetime import timedelta

    from sqlalchemy import select

    from papertrader.db.models import JobRun
    from papertrader.db.types import utcnow
    from papertrader.ingest.timeutil import NEW_YORK

    jobs = _jobs()
    query = select(JobRun).where(JobRun.scheduled_for >= utcnow() - timedelta(days=days))
    if not monitor:
        query = query.where(JobRun.job_name != "monitor")
    with jobs.sessions() as session:
        runs = session.scalars(query.order_by(JobRun.scheduled_for)).all()
        for run in runs:
            took = (f"{(run.finished_at - run.started_at).total_seconds():.0f}s"
                    if run.finished_at and run.started_at else "")  # fmt: skip
            summary = run.error or (run.details or {}).get("summary", "")
            typer.echo(f"{run.scheduled_for.astimezone(NEW_YORK):%a %d %b %H:%M}  {run.job_name:<18}"
                       f"{run.status.value:<10}{took:>6}  {str(summary)[:110]}")  # fmt: skip
    if not runs:
        typer.echo("No job runs yet. They start once the app is running: docker compose up -d")


def _split(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


def _ingest(mode: str, **options: object) -> None:
    """Shared body of the ingest commands: run the steps, print a summary table."""
    from papertrader.ingest.context import open_context
    from papertrader.ingest.pipeline import (
        STEPS,
        SelectionError,
        StepResult,
        coverage,
        run_backfill,
        run_daily,
    )
    from papertrader.runtime import bootstrap

    only = options.get("only")
    if isinstance(only, list):
        unknown = sorted(set(only) - set(STEPS))
        if unknown:
            raise _fail(f"unknown step(s) {', '.join(unknown)}. Steps: {', '.join(STEPS)}")
    try:
        # These commands are run by a person at a terminal: plain lines, not JSON.
        runtime = bootstrap(migrate=True, log_format="console")
    except (ConfigError, LiveTradingForbiddenError) as exc:
        raise _fail(str(exc)) from exc

    async def go() -> list[StepResult]:
        async with open_context(runtime.config, runtime.settings, runtime.session_factory) as ctx:
            if mode == "status":
                for name, rows, span in coverage(ctx):
                    typer.echo(f"{name:<24}{rows:>10,}  {span}")
                return []
            if mode == "daily":
                return await run_daily(ctx, only=only)  # type: ignore[arg-type]
            return await run_backfill(ctx, **options)  # type: ignore[arg-type]

    try:
        results = asyncio.run(go())
    except SelectionError as exc:
        raise _fail(str(exc)) from exc
    if not results:
        return

    typer.echo(f"\n{'step':<14}{'result':<9}{'new/changed':>12}{'requests':>10}{'time':>8}  notes")
    for result in results:
        colour = {"ok": typer.colors.GREEN, "failed": typer.colors.RED}.get(result.status)
        line = (
            f"{result.name:<14}{result.status:<9}{result.rows:>12,}{result.requests:>10,}"
            f"{result.seconds:>7.0f}s  {result.detail}"
        )
        typer.secho(line, fg=colour)
    if any(result.status == "failed" for result in results):
        raise _fail("\nSome steps failed. They can be re-run safely; finished work is kept.")
    typer.echo("\nDone. Running the same command again only fetches what is new.")


@ingest_app.command("backfill")
def ingest_backfill(
    years: Annotated[
        int | None, typer.Option(help="Years of history (default: backfill.years in config.yaml).")
    ] = None,
    tickers: Annotated[
        str | None, typer.Option(help="Only these tickers, comma-separated, e.g. AAPL,MSFT,JPM.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option(help="Only this many tickers, spread across sectors.")
    ] = None,
    only: Annotated[
        str | None, typer.Option(help="Only these steps, comma-separated (see `ingest steps`).")
    ] = None,
) -> None:
    """Load historical data. Safe to interrupt and re-run: it continues where it stopped."""
    from papertrader.config import get_config

    try:
        default_years = get_config().backfill.years
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    _ingest(
        "backfill", years=years or default_years, tickers=_split(tickers), limit=limit,
        only=_split(only),
    )  # fmt: skip


@ingest_app.command("daily")
def ingest_daily(
    only: Annotated[str | None, typer.Option(help="Only these steps, comma-separated.")] = None,
) -> None:
    """Fetch everything new since the last run, for the whole universe."""
    _ingest("daily", only=_split(only))


@ingest_app.command("status")
def ingest_status() -> None:
    """Show how much data is stored and which dates it covers."""
    _ingest("status")


@ingest_app.command("steps")
def ingest_steps() -> None:
    """List the ingestion steps and the key each one needs."""
    needs = {
        "universe": "none (SEC_USER_AGENT adds SEC identifiers)",
        "cusip_map": "SEC_USER_AGENT", "bars": "ALPACA keys", "news": "ALPACA keys",
        "finnhub_news": "FINNHUB_API_KEY (optional)", "filings": "SEC_USER_AGENT",
        "insiders": "SEC_USER_AGENT", "holdings_13f": "SEC_USER_AGENT",
        "earnings": "FINNHUB_API_KEY (optional)", "macro": "FRED_API_KEY (optional)",
        "market_caps": "FINNHUB_API_KEY (optional)", "dedupe": "none",
    }  # fmt: skip
    for name, need in needs.items():
        typer.echo(f"{name:<14}{need}")


if __name__ == "__main__":
    app()
