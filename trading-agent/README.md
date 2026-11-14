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
| 2 | Data ingestion + historical backfill | **done** (needs your test run, see below) |
| 3 | Research agents + judge + `dry-run` | **done** (needs your real-API test, see below) |
| 4 | Pattern model + backtest report | not started |
| 5 | Trading engine + risk rules | not started |
| 6 | Telegram | not started |
| 7 | Learning loop + scheduler | not started |
| 8 | Dashboard + deployment guide | not started |
| 9 | Run on paper for 4-8 weeks | not started |

Right now the program can load and refresh all its data and run the research agents
and the judge to produce a decision. It does not place orders yet.

## Decisions in effect

| Decision | Choice |
|---|---|
| Hosting | Your own computer, with Docker |
| Stocks | S&P 500 |
| Frequency | One research cycle before the market opens; positions checked every 10 minutes |
| Claude API budget | $100 per month, enforced in code |
| Models | Judge: `claude-opus-5-5`. Research agents: `claude-sonnet-5-5` (changeable per agent) |

All of these live in [`config.yaml`](config.yaml), with comments.

## Start it (Docker, Windows 10)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
Open **PowerShell** in this folder (`trading-agent`): in File Explorer, open the folder,
click the address bar, type `powershell` and press Enter.

1. Create your private settings file (you can leave the keys empty for now):

   ```bash
   copy .env.example .env
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

## Load data (Phase 2)

Put your keys in `.env` first (see "Your keys" below), then rebuild once so the
containers have the new code:

```bash
docker compose up -d --build
```

**Test run with 20 stocks.** This is the Phase 2 acceptance test. It picks 20 stocks
spread across sectors and loads 5 years of history. Expect roughly 20 to 40 minutes;
most of that is news and insider filings.

```bash
docker compose run --rm app papertrader ingest backfill --limit 20
```

It prints one line per step: `ok`, `skipped` (a key is missing, with the reason) or
`failed` (with the error). **Run the same command a second time**: it should finish
in about a minute and report almost nothing new. That is what "idempotent" means, and
it is also how you resume if the first run is interrupted (closing the window, losing
the connection): nothing is lost, it carries on from where it stopped.

See what is stored:

```bash
docker compose run --rm app papertrader ingest status
```

Other commands:

```bash
docker compose run --rm app papertrader ingest steps
```

```bash
docker compose run --rm app papertrader ingest backfill --tickers AAPL,MSFT,JPM --years 2
```

```bash
docker compose run --rm app papertrader ingest backfill --only filings,insiders
```

```bash
docker compose run --rm app papertrader ingest daily
```

**Full S&P 500 backfill** (the same command without `--limit`) takes several hours,
mostly because the SEC and Alpaca limit how fast data may be requested. It is safe to
stop and restart. Do the 20-stock test first; the full load is only needed before
Phase 9.

**If the `universe` step mentions HTTP 403:** Wikipedia refused the request for the
S&P 500 member list. The program then uses its built-in list (dated 2026-11-13), so
everything still works; only index changes after that date are missing. To get the live
list, open `.env`, set `WIKIPEDIA_CONTACT=` to an email address or web page of yours
(Wikipedia asks automated tools to say who they are) and run the command again. This is
optional, and nothing personal is sent to Wikipedia unless you set it.

What each step loads:

| Step | Source | Key needed |
|---|---|---|
| `universe` | S&P 500 members, sectors (Wikipedia, or the built-in list), SEC identifiers | none |
| `cusip_map` | Security codes, needed to read fund holdings (SEC) | `SEC_USER_AGENT` |
| `bars` | Daily prices, adjusted for splits and dividends (Alpaca) | Alpaca |
| `news` | News, mapped to tickers (Alpaca) | Alpaca |
| `finnhub_news` | Second news source, last few days only | Finnhub (optional) |
| `filings` | 8-K, 10-Q, 10-K, Form 4 lists; earnings release times (SEC) | `SEC_USER_AGENT` |
| `insiders` | Insider trades from each Form 4, last 2 years (SEC) | `SEC_USER_AGENT` |
| `holdings_13f` | Quarterly holdings of 15 large funds (SEC) | `SEC_USER_AGENT` |
| `earnings` | EPS and revenue, actual vs estimate (Finnhub) | Finnhub (optional) |
| `macro` | Rates, CPI, unemployment, yield curve (FRED) | FRED (optional) |
| `market_caps` | Market capitalisation (Finnhub) | Finnhub (optional) |
| `dedupe` | Marks the same news story arriving twice | none |

## Run the research agents (Phase 3)

`dry-run` runs every research agent and the judge once and prints the full decision:
each agent's reports, what was removed for lacking a source, the judge's ranked trades
with their confidence, and what the run cost. **Nothing is traded.**

First see what a run would cost, without calling Claude:

```bash
docker compose run --rm app papertrader dry-run --estimate-only
```

Then a real run, which needs `ANTHROPIC_API_KEY` in `.env`. `--max-cost` is a hard
limit in dollars for this one run:

```bash
docker compose run --rm app papertrader dry-run --max-cost 2
```

A run on the 20 test stocks costs roughly $1; on the full S&P 500 expect $1.50 to
$2.50. Every call is logged with its cost and counted against the monthly budget.

Other forms:

```bash
docker compose run --rm app papertrader dry-run --agents macro,technicals
```

```bash
docker compose run --rm app papertrader dry-run --date 2026-11-12
```

A run with `--date` uses only data that was public at 08:20 New York time on that day.
It prints a warning, and it is right to: Claude was trained on text that may describe
what happened afterwards, so **results on past dates are optimistic and prove nothing**.
Only forward paper trading is a real test.

The agents:

| Agent | Looks at |
|---|---|
| `macro` | Rates, yield curve, inflation, jobs, credit spreads; gives sector views |
| `earnings` | Results just reported and reports due, with the price reaction |
| `sec_filings` | New 8-K filings (item codes and opening text), new 10-Q/10-K |
| `sector_analyst` (one per sector, 11) | Stocks in the sector with fresh news, filings or earnings |
| `news_event` | Fresh headlines, classified by event type |
| `technicals` | Momentum, RSI, volume spikes, gaps, 52-week levels (numbers, not charts) |
| `insider_institutional` | Insider buying and selling, changes in fund holdings |
| `contrarian` | Tries to disprove the other agents' best ideas |

Agents with nothing new to look at are skipped and cost nothing. Each agent's model and
effort can be changed in `config.yaml` under `agents.overrides`.

## Your keys

Keys go in `.env`, never anywhere else. `.env` is ignored by git, so it cannot be committed
by accident. `papertrader check` shows which keys are present (names only, never values).

| Key | Needed from | Where to get it |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Phase 2 | Alpaca dashboard, **Paper** account. Paper keys start with `PK` |
| `SEC_USER_AGENT` | Phase 2 | Just your name and email, e.g. `Jane Doe jane@example.com` |
| `FRED_API_KEY` (optional) | Phase 2 | Free, from the St. Louis Fed |
| `WIKIPEDIA_CONTACT` (optional) | Phase 2 | Your own email or web page; only if Wikipedia refuses the member list |
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
  ingest/              data loading: one file per source, plus pipeline.py
  safety.py            the paper-only guard
  config.py            reads and validates config.yaml and .env
  logging_setup.py     JSON logs
  runtime.py           startup checks
  cli.py               the `papertrader` command
  db/                  tables, migrations, sessions
  scheduler/           the long-running service (heartbeat only, for now)
  dashboard/           Streamlit status page
  agents/ judge/ model/ trading/ notify/ learning/   built in later phases
tests/                 automated tests
```

How the pieces fit together is described in [ARCHITECTURE.md](ARCHITECTURE.md).
