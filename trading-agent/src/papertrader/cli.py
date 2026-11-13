"""Command line entry point: `papertrader <command>`."""

from __future__ import annotations

import asyncio
from typing import Annotated

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
        runtime = bootstrap(migrate=True)
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
