# papertrader

A multi-agent stock research system that **paper-trades** on Alpaca and reports to you on Telegram.

> **This is an experiment, not financial advice, and there is no promise of profit.**
> It only ever trades with pretend money on Alpaca's paper account. There is no
> live-trading code in this project, and the program refuses to start if anything
> points it at a real-money endpoint.

## What it will do (when all phases are built)

1. Collect news, SEC filings, earnings, macro data and prices.
2. Run ~18 specialised research agents (Claude) that each write a structured report.
3. Score the reports with a pattern model trained on past news and price reactions.
4. A judge agent checks everything and picks the highest-confidence trades.
5. Place paper trades on Alpaca under strict risk limits.
6. Send Telegram messages: morning brief, trades, exits, daily and weekly summaries.
7. Score every past call against what actually happened and adjust.

## Build status

| Phase | What | Status |
|---|---|---|
| 1 | Scaffold: config, database, logging, CI, Docker | **done** |
| 2 | Data ingestion + historical backfill | not started |
| 3 | Research agents + judge + `dry-run` | not started |
| 4 | Pattern model + backtest report | not started |
| 5 | Trading engine + risk rules | not started |
| 6 | Telegram | not started |
| 7 | Learning loop + scheduler | not started |
| 8 | Dashboard + deployment guide | not started |
| 9 | Run on paper for 4-8 weeks | not started |

Right now the program starts, checks its configuration, creates its database and
shows a status page. It does not fetch data, call Claude or place orders yet.

## Decisions in effect

| Decision | Choice |
|---|---|
| Hosting | Your own computer, with Docker |
| Stocks | S&P 500 |
| Frequency | One research cycle before the market opens; positions checked every 10 minutes |
| Claude API budget | $100 per month, enforced in code |
| Models | Judge: `claude-opus-5-5`. Research agents: `claude-sonnet-5-5` (changeable per agent) |

All of these live in [`config.yaml`](config.yaml), with comments.

## Start it (Docker)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
Open a terminal in this folder (`trading-agent`).

1. Create your private settings file (you can leave the keys empty for now):

   ```bash
   cp .env.example .env
   ```

2. Open `.env` in a text editor and change `POSTGRES_PASSWORD` to something of your own
   (letters and digits only). Do this before the first start: the database remembers
   the password it was first created with.

3. Build and start:

   ```bash
   docker compose up -d --build
   ```

4. Open the status page: <http://localhost:8501>

Useful commands:

```bash
docker compose ps
```

```bash
docker compose logs -f app
```

```bash
docker compose exec app papertrader check
```

```bash
docker compose down
```

`docker compose down` stops everything and keeps your data. (Adding `-v` would delete the
database, so don't, unless you want to start from zero.)

After editing `config.yaml`, apply it with `docker compose restart`.

## Your keys

Keys go in `.env`, never anywhere else. `.env` is ignored by git, so it cannot be committed
by accident. `papertrader check` shows which keys are present (names only, never values).

| Key | Needed from | Where to get it |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Phase 2 | Alpaca dashboard, **Paper** account. Paper keys start with `PK` |
| `SEC_USER_AGENT` | Phase 2 | Just your name and email, e.g. `Jane Doe jane@example.com` |
| `FRED_API_KEY` (optional) | Phase 2 | Free, from the St. Louis Fed |
| `FINNHUB_API_KEY` (optional) | Phase 2 | Free tier at finnhub.io |
| `ANTHROPIC_API_KEY` | Phase 3 | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Phase 6 | Telegram's @BotFather |

## Safety rules built in

- **Paper only.** The Alpaca paper address is hard-coded in `src/papertrader/safety.py`.
  At every start the program checks it, checks that no environment variable redirects
  trading elsewhere, and rejects API keys that look like live-account keys.
- **No leverage, no options.** There is no setting for them. A config whose limits would
  need more than 100% of the account is rejected.
- **Budget.** Every Claude call will be logged with its cost; the system does less work as
  the monthly budget runs out instead of overspending (Phase 3).
- **Secrets** are never logged; fields that look like keys are masked in log output.

## For development (without Docker)

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run papertrader check
```

```bash
uv run papertrader db upgrade
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Without Docker the program uses a local SQLite file (`data/papertrader.db`).
Tests never touch the network and need no keys.

## Layout

```
config.yaml            behaviour: schedule, risk limits, models, budget (no secrets)
.env.example           template for your private keys
docker-compose.yml     db + app + dashboard
src/papertrader/
  safety.py            the paper-only guard
  config.py            reads and validates config.yaml and .env
  logging_setup.py     JSON logs
  runtime.py           startup checks
  cli.py               the `papertrader` command
  db/                  tables, migrations, sessions
  scheduler/           the long-running service (heartbeat only, for now)
  dashboard/           Streamlit status page
  ingest/ agents/ judge/ model/ trading/ notify/ learning/   built in later phases
tests/                 automated tests
```

How the pieces fit together is described in [ARCHITECTURE.md](ARCHITECTURE.md).
