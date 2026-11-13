"""Command line entry point: `papertrader <command>`."""

from __future__ import annotations

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


if __name__ == "__main__":
    app()
