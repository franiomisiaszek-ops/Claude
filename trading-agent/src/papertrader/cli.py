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
