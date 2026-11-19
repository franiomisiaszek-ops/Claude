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
| 3 | Research agents + judge + `dry-run` | **done** (real API verified; a run on real prices still to do) |
| 4 | Pattern model + backtest report | **done** (trained on synthetic data only so far, see below) |
| 5 | Trading engine + risk rules | **done** (needs your test on the paper account, see below) |
| 6 | Telegram messages + bot commands | **done** (needs your test, see below) |
| 7 | Learning loop + scheduler | **done** (runs by itself once the app is up, see below) |
| 8 | Dashboard + deployment guide | **done** (see [docs/WINDOWS.md](docs/WINDOWS.md)) |
| 9 | Run on paper for 4-8 weeks | not started |

Right now the program can load and refresh all its data, run the research agents and the
judge, trade their decisions on the Alpaca paper account, send everything to you on
Telegram, and run all of it by itself every trading day while the app is up. It scores
its own past calls and learns from them each week. A dashboard shows all of it, and a
backup is made every night. Next is the paper run itself (Phase 9).

**Running it day to day (power settings, backups, updates, troubleshooting):
[docs/WINDOWS.md](docs/WINDOWS.md).**

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

The full step-by-step guide, from installing Docker to backups and updates, is
[docs/WINDOWS.md](docs/WINDOWS.md). The short version:

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

Measured on 2026-11-14 against the real API: a full run on the 20 test stocks cost
about $0.10 (the estimate shown beforehand was $0.35; estimates are deliberately high).
A run on the full S&P 500 on a busy news day will cost more; it has not been measured
yet. Every call is logged with its cost and counted against the monthly budget.

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

## Train the pattern model (Phase 4)

The pattern model learns from history how stocks moved after news, filings, earnings and
insider trades, and gives the judge a probability that a stock beats the market over the
next five days. Training needs no API key and costs nothing.

```bash
docker compose run --rm app papertrader model train
```

This builds the dataset from everything loaded so far, tests the model the honest way
(always on days later than the ones it learned from), runs a backtest against two
baselines, and writes a report. Open the report in the `reports` folder next to this
file (`..._backtest_report.html`), or print it:

```bash
docker compose run --rm app papertrader model report
```

```bash
docker compose run --rm app papertrader model status
```

How to read the result:

- The report starts with warnings. Read them. A backtest replays the past and always
  looks better than real trading.
- **"Too little data"** appears until much more history is loaded. Twenty stocks is
  enough to prove the machinery works, not to prove anything about markets.
- **"Not distinguishable from random picks"** means exactly that: choosing at random
  from the same stocks on the same days did about as well.
- **"These results look too good"** means a bug or leaked future information is the
  likely explanation, not a discovery.
- A model is only switched on ("promoted") if it shows measurable skill on days it never
  saw and beats the model already in use. With no model switched on, the judge decides
  on the agents' reports alone. That is the normal, honest state until a model earns it.

Optional: text features from Claude. Historical headlines can be labelled by Claude
(event type, sentiment, importance). To stop Claude from "remembering" what a stock did
next, it only sees masked text: company names, tickers and dates are replaced by
placeholders. This uses the cheapest model at half price and is not immediate:

```bash
docker compose run --rm app papertrader model text-submit --limit 20000
```

```bash
docker compose run --rm app papertrader model text-collect
```

Run the second command later (usually within the hour), then train again. Masking
reduces the leak but cannot remove it; the report says how many rows used these features.

## Paper trading (Phase 5)

Every position is opened with a **bracket order**: the buy plus a take-profit and a
stop-loss that sit at Alpaca. They trigger even when this program is not running.
The program adds the exits Alpaca cannot know about: selling when the planned holding
period ends, when bad news breaks the reason for the trade, or when you close by hand.

Run these while the US market is open: 09:35-16:00 New York time, which is usually
15:35-22:00 in Poland (for a week or two in spring and autumn the clocks change on
different dates and it is one hour earlier).

**The acceptance test: a compressed trading day.** It buys one share of SPY, watches
it for 20 minutes, sells it again, and writes the end-of-day summary:

```bash
docker compose run --rm app papertrader trade day
```

Add `--research` to run the agents and judge first and trade whatever they approve
(costs about $1-2 in API fees). `--minutes` and `--interval` change how long and how
often it checks.

Everyday commands:

```bash
docker compose run --rm app papertrader trade status
```

```bash
docker compose run --rm app papertrader trade place
```

```bash
docker compose run --rm app papertrader trade monitor
```

```bash
docker compose run --rm app papertrader trade close AAPL
```

```bash
docker compose run --rm app papertrader trade reconcile
```

```bash
docker compose run --rm app papertrader trade test-order AAPL
```

```bash
docker compose run --rm app papertrader trade pause
```

```bash
docker compose run --rm app papertrader trade resume
```

- `status`: equity, positions, open orders, whether trading is paused.
- `place`: places today's approved trades (from a `daily` research run; `dry-run`
  decisions are never traded).
- `monitor`: records fills, sells positions whose time is up, checks the daily loss
  limit and reviews held stocks that get bad news. `--loop` repeats until the close.
- `close`: sells one position now.
- `reconcile`: end of day; cancels buy orders that did not fill.
- `test-order`: buys one share with a +2% target and a -2% stop, sold at the close.
  Test trades are marked and never count in the statistics.
- `pause` / `resume`: stop or allow new orders. Open positions keep their stops.

These commands also print the messages they queue for Telegram (see the next section).

**Risk limits** (all in `config.yaml`): at most 10% of the account per position, 10
positions, 30% per sector; no leverage (only uncommitted cash is used), no options,
no shorts. **Kill switch**: if the account is down 3% on the day, trading pauses,
unfilled buy orders are cancelled, open positions keep their stop-losses, and it stays
paused until you resume it.

## Telegram (Phase 6)

The program sends you messages and answers commands through your own Telegram bot.
Setting it up takes five minutes and is free.

**1. Create the bot.** In Telegram, open **@BotFather**, send `/newbot`, pick a name and
a username ending in `bot`. BotFather answers with a token like `123456789:AAE...`.
Put it in `.env`:

```
TELEGRAM_BOT_TOKEN=123456789:AAE...
```

Treat the token like a password: anyone who has it can send messages as your bot.

**2. Find your chat id.** Open your new bot in Telegram and press **Start** (or send
"hi"). Then run:

```bash
docker compose run --rm app papertrader telegram setup
```

It prints a line like `TELEGRAM_CHAT_ID=987654321`. Put that line in `.env` as well.
The bot only ever answers this chat; messages from anyone else are ignored and you get
an alert about them.

**3. Restart and test.**

```bash
docker compose up -d --build
```

```bash
docker compose run --rm app papertrader telegram test
```

You should receive seven messages marked `[TEST]`, one of each type: morning brief,
trade executed, sell signal, trade closed, end of day, weekly summary, alert. They are
samples with made-up numbers. Then send `/status` to the bot: the running app container
answers within a few seconds.

**Bot commands** (only from your chat):

| Command | What it does |
|---|---|
| `/status` | Trading active or paused, kill switch, market open, API spend, last research run, heartbeat |
| `/positions` | Open positions with entry price, current price and gain |
| `/pnl` | Equity today and since the start, against SPY; closed trades and win rate |
| `/brief` | Today's morning brief again |
| `/pause [reason]` | Stop new orders. Open positions keep their stop-loss and take-profit |
| `/resume` | Allow new orders again (also clears the kill switch) |
| `/help` | The list above |

A command that reaches the bot more than 15 minutes late (for example because the app
was off) is not carried out; the bot says so and you can send it again.

**How sending works.** Every message is saved in the database first and sent by the
app every 15 seconds, in order. If Telegram or your internet is down, messages wait and
go out later; anything older than 24 hours is dropped instead of arriving late. If the
token or chat id is wrong, nothing is lost: messages wait until you fix `.env`.

Other commands:

```bash
docker compose run --rm app papertrader telegram send
```

```bash
docker compose run --rm app papertrader telegram brief
```

```bash
docker compose run --rm app papertrader telegram weekly
```

`send` sends waiting messages now, `brief` sends today's morning brief (needs today's
research run), `weekly` sends the weekly summary. The app sends the brief (08:45), the
end-of-day summary (16:15) and the weekly summary (Saturday) by itself.

## The daily schedule and the learning loop (Phase 7)

Once the app is running (`docker compose up -d`), it does everything on its own, on
trading days only (the Alpaca market calendar knows the holidays and half days):

| New York | Poland* | Job | What happens |
|---|---|---|---|
| 06:30 | 12:30 | `ingest` | Overnight news, filings, earnings, macro, prices. New headlines are sent for cheap Claude labels |
| 07:30 | 13:30 | `research_agents` | The research agents write their reports |
| 08:20 | 14:20 | `judge` | Pattern-model scores, then the judge decides |
| 08:45 | 14:45 | `morning_brief` | Telegram morning brief |
| 09:35 | 15:35 | `place_orders` | Sells what the judge wants out of, then places today's orders |
| every 10 min | | `monitor` | Fills, time stops, the kill switch, bad news on held stocks |
| 16:15 | 22:15 | `reconcile_eod` | Cancels unfilled orders, end-of-day summary |
| 17:00 | 23:00 | `resolve_outcomes` | Scores every agent call whose holding period has ended |
| Sat 09:00 | Sat 15:00 | `weekly` | New agent weights, model retrain, lessons, weekly summary |

\*Poland is usually 6 hours ahead of New York. For a week or two in spring and autumn
the clocks change on different dates and it is 5 hours.

**Your computer must be on and awake** at those times, with Docker Desktop running. In
Windows: Settings > System > Power & sleep > Sleep: "Never" (when plugged in), and in
Docker Desktop: Settings > General > "Start Docker Desktop when you sign in". The app and
the database restart by themselves after a reboot. [docs/WINDOWS.md](docs/WINDOWS.md),
section 3, has the details (Windows Update hours, signing in after a restart).

**If something does not run, you hear about it.** A job that fails sends an alert. Every
five minutes a watchdog checks that each of today's jobs ran (15 minutes of grace), and
that the monitor is running while the market is open. When the app starts after being
off for a while, it tells you for how long. Missed jobs are not run late by themselves
(a morning brief at 3 pm would be confusing); run one by hand if you want it:

```bash
docker compose run --rm app papertrader jobs list
```

```bash
docker compose run --rm app papertrader jobs run judge
```

```bash
docker compose run --rm app papertrader jobs history
```

`list` shows the schedule and how each job went today, `run NAME` runs one now (also on a
weekend or holiday), `history` shows the recent runs with their results.

**How it learns.**

1. **Every evening**, each agent call whose holding period has ended is scored the same way
   the pattern model's history is: from the open after the call to the close at the end of
   its horizon, against SPY. A "long" call is right if the stock beat SPY; a "short" call if
   it lagged. Only live calls count: a `dry-run` on a past date is never scored, because
   Claude may already know what happened next.
2. **Accuracy per agent** is kept for the last 30, 90 and 365 days, also per sector and
   per event type: hit rate, Brier score (how good the confidence figures are) and average
   excess return.
3. **Every Saturday** each agent gets a reliability weight:
   `1 + 4 x (hit rate - 50%) x n / (n + 30)`, between 0.25 and 2.0, from its last 90 days.
   With fewer than 10 resolved calls it stays 1.0. The `n / (n + 30)` part means a lucky
   streak of a few calls barely moves an agent. The judge sees these weights, and they set
   how much each agent counts in the "agent agreement" part of the final confidence.
4. **The pattern model is retrained** on the newest data and only replaces the active
   model if it does better on the latest unseen weeks.
5. **The judge writes "lessons"** from the past four weeks of resolved calls and closed
   trades (one Claude call, about $0.10-0.30) and reads them before every decision for the
   next three weeks. Below 10 resolved calls it writes nothing; with few calls it is told to
   say the evidence is thin.

Expect nothing to be scored in the first week: a 5-day call made on Monday can only be
scored the following Monday evening. Weights and lessons need a few weeks of calls before
they mean anything. All the numbers are in `config.yaml` under `learning:`.

**Cost.** A daily run on the full S&P 500 has not been measured yet (the 20-stock test run
cost about $0.10); the first week will show it, in `/status` and the weekly summary.
Headline labels cost a few cents a day, the lessons $0.10-0.30 a week. Whatever happens,
the $100 monthly budget is enforced in code: each day may spend about what is left for
the month divided by the trading days left, and close to the limit fewer agents run.

## Dashboard (Phase 8)

<http://localhost:8501>, only from this PC. Read-only: nothing on it can change the system.

| Page | What it shows |
|---|---|
| Overview | Equity, return since start vs SPY, open trades, win rate, API spend, whether the app is running, paused or stopped by the kill switch, today's jobs |
| Equity vs SPY | Return since start against SPY, and the drawdown (how far below the best level so far) |
| Trades | Open and closed trades: prices, P&L, how they ended, which agents were right |
| Decisions and reports | Any day's decision: market summary, ideas with every part of the confidence, rejected ideas, and every agent report with its evidence and sources |
| Agent leaderboard | Hit rate, Brier score and return per agent (30/90/365 days), by sector and event type; the weights the judge uses; the latest lessons |
| Pattern model | Every trained version with its out-of-sample results and why it was or wasn't used |
| API spend | Spend per day, by purpose and model, against the budget |
| Jobs and messages | Every scheduled run and every Telegram message |

## Backups (Phase 8)

A `backup` container saves the whole database and the trained models every night
(about 06:00 Polish time) into the `backups` folder and keeps the newest 7. The app
alerts you on Telegram if backups stop. A backup right now:

```bash
docker compose exec backup sh /scripts/backup.sh now
```

How to keep a copy in OneDrive, restore a backup, and update safely:
[docs/WINDOWS.md](docs/WINDOWS.md), sections 5-7.

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
docker-compose.yml     db + app + dashboard + backup
docs/WINDOWS.md        running it on your PC: power, backups, updates, troubleshooting
scripts/               backup.sh and restore.sh (run inside the backup container)
src/papertrader/
  ingest/              data loading: one file per source, plus pipeline.py
  safety.py            the paper-only guard
  config.py            reads and validates config.yaml and .env
  logging_setup.py     JSON logs
  runtime.py           startup checks
  cli.py               the `papertrader` command
  db/                  tables, migrations, sessions
  scheduler/           the long-running service: the schedule, jobs, watchdog
  dashboard/           Streamlit pages (read-only), queries and charts
  trading/             broker adapter (paper only), risk rules, engine, thesis checks
  notify/              message outbox, texts, Telegram sender and bot
  learning/            scoring past calls, agent weights, weekly lessons
tests/                 automated tests
```

How the pieces fit together is described in [ARCHITECTURE.md](ARCHITECTURE.md).
