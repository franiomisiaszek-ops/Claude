# Architecture

This document explains how the system is put together and why. It is updated at the end
of every phase. Sections marked *(planned)* describe the design for phases not built yet.

## The daily flow

```
06:30  ingest      news, filings, earnings, macro, prices  ->  database
07:30  agents      ~18 Claude research agents, in parallel ->  agent_reports
08:20  model+judge pattern-model scores, then the judge    ->  judge_decisions, trade_ideas
08:45  notify      Telegram morning brief
09:35  trading     risk checks, bracket orders (PAPER)     ->  orders, fills, trades
 ...   monitor     every 10 minutes: exits, kill switch
16:15  reconcile   end-of-day summary                      ->  equity_snapshots
17:00  learning    score past predictions                  ->  prediction_outcomes, agent_stats
Sat    learning    re-weight agents, retrain model, weekly report
```

All times are New York time on trading days. Each step reads from and writes to the
database, so steps are independent: one can fail and be re-run without redoing the others.

## What exists today (Phases 1-3)

| Piece | File | What it does |
|---|---|---|
| Paper-only guard | `safety.py` | Hard-coded paper URL; refuses live URLs, env overrides and live-looking keys |
| Configuration | `config.py`, `config.yaml` | Validated settings. Unknown keys and out-of-range values are errors |
| Secrets | `config.py` (`Settings`), `.env` | Loaded from the environment; wrapped so they cannot be printed |
| Logging | `logging_setup.py` | One JSON object per line, UTC timestamps, secret-looking fields masked |
| Database schema | `db/models.py` | All 31 tables for every phase |
| Migrations | `db/migrations/` | Alembic; runs automatically when the app starts |
| Startup | `runtime.py` | Guard first, then config, then database |
| CLI | `cli.py` | `papertrader check`, `db upgrade`, `run`, `version` |
| Service | `scheduler/service.py` | APScheduler in New York time; only a heartbeat job so far |
| Dashboard | `dashboard/` | Read-only Streamlit status page |
| Health check | `healthcheck.py` | Docker marks the app unhealthy if the heartbeat stops |
| Ingestion | `ingest/` | Loads and refreshes every data source (see below) |
| Claude access | `llm/` | One client, cost per call, the budget governor |
| Research agents | `agents/` | Point-in-time snapshot, 8 agent types, citation checks |
| Judge | `judge/` | Weighs reports, proposes trades; code checks and scores them |
| Research cycle | `research/` | Runs agents, contrarian and judge; `dry-run` output |

## Ingestion (Phase 2)

```
ingest/
  http.py        rate limit + retries with backoff, one Fetcher per source
  store.py       upsert (insert-or-update) and resume bookmarks
  timeutil.py    New York <-> UTC, when a daily bar or filing counts as "known"
  universe.py    index members, sectors, SEC identifiers, market cap
  alpaca_data.py Alpaca data through the official alpaca-py SDK (data only, no trading)
  bars.py        price bars
  news.py        news, ticker mapping, de-duplication
  sec.py         filing lists, Form 4 insider trades, 13F holdings, CUSIP map
  earnings.py    Finnhub earnings calendar
  macro.py       FRED series, with revision history
  pipeline.py    runs the steps for a backfill or the daily refresh
```

How the requirements are met:

- **Idempotent.** Every write is an upsert on a natural key (ticker + time, source + id,
  SEC accession number...). Running anything twice changes nothing.
- **Resumable.** `ingest_state` holds a bookmark per source and ticker. Work is committed
  in small units (a chunk of tickers, a month of news, a batch of filings), so an
  interrupted backfill continues from the last finished unit.
- **Timestamps.** Every row has `available_at` in UTC, chosen conservatively:

  | Data | `available_at` |
  |---|---|
  | Daily bar | 16:15 New York on that day (close + 15-minute delay); today's unfinished bar is never stored |
  | News | last edit time of the article, because an edited article may contain later facts |
  | SEC filing, insider trade, 13F holding | EDGAR acceptance time (true UTC, verified against live filings) |
  | Earnings (Finnhub) | 09:30 for "before open", 20:00 New York for "after close" or unknown: late on purpose |
  | Earnings (8-K item 2.02) | exact acceptance time of the filing |
  | Macro, revised series | the day that version was published, 10:00 New York |
  | Macro, market series | the next day, 18:00 New York |

- **De-duplication.** `content_hash` is a hash of the headline with case and punctuation
  removed. The earliest copy within 72 hours is the original; later copies (another
  source, or a repost) get `duplicate_of_id`. Only exact headline matches are caught,
  not rewordings.
- **Ticker mapping.** News keeps the symbols the source gives. Filings map through the
  SEC identifier (CIK). 13F holdings only carry CUSIP codes, so a CUSIP-to-ticker map is
  built from the SEC's fails-to-deliver files (no key needed).
- **Split and dividend safety.** Prices are stored adjusted. Each run re-reads the last 7
  days; if a stored price no longer matches, the adjusted history changed and that
  ticker's whole history is re-read.
- **SEC rules.** Every request carries `SEC_USER_AGENT`; the rate is capped at 8 per
  second (the SEC allows 10).
- **Secrets.** FRED requires the key in the URL, so URLs are stripped of keys in error
  messages and in third-party log lines.
- **The universe never goes missing.** The member list comes from Wikipedia. If that
  fails (refused, down, or layout changed), an existing list is kept untouched; an empty
  database is filled from the list shipped in `ingest/snapshots/`. Steps that work per
  stock are reported as skipped, never as "ok", if there is no stock list.
- **Failure isolation.** A missing key skips its step. A failing step is reported and the
  others still run. One bad ticker or filing never stops a step.

Backfill vs daily: the backfill reaches back `backfill.years`. The daily run only looks
a short way back (news 7 days, filings 30, Form 4 details 14, 13F 200) so that it always
finishes within the hour, even if the full backfill was never run.

Known limits (also relevant for the Phase 4 backtest):

- **Survivorship bias.** Only today's index members are loaded. Companies that left the
  index in the last five years are missing, which makes any backtest look better than
  reality. The table has join/leave dates for members seen from now on.
- **Adjusted prices are today's view.** A past price is stored as adjusted by all later
  splits and dividends. Returns are right; absolute past price levels are not what was
  on screen that day.
- **Finnhub history** on the free tier may be shorter than five years; whatever Finnhub
  returns is stored.
- **Earnings calendar dates for the past** carry no record of when the date was
  announced, so past entries only count as known once results were out.
- **Form 4 details** are read for the last 2 years by default (`ingest.form4_detail_years`);
  a full S&P 500 load is roughly 75,000 documents, about 3 hours at the SEC's rate limit.
- **13F** covers 15 chosen funds, share positions only (no options or bonds), and is by
  nature 45 days old when published.
- **Parquet** is not used yet. Prices fit comfortably in the database; the Phase 4
  training dataset will be written as Parquet.

## Research agents and judge (Phase 3)

```
agents/snapshot.py   reads everything as of one moment (the only database access)
agents/roster.py     turns the snapshot into each agent's context, every item with an id
llm/client.py        one structured Claude call: budget check, call, cost log
agents/validate.py   deletes claims without a valid citation
research/cycle.py    agents in parallel -> contrarian -> judge -> stored decision
judge/judge.py       builds the judge's context; checks, scores and ranks its trades
judge/blend.py       the final-confidence formula
```

**Point in time.** `build_snapshot()` is the single gate: every query filters on
`available_at <= as_of`. Agents never query the database, so a run "as of" a past moment
cannot see later news, prices, filings, results or macro revisions. Tests seed data on
both sides of the decision time and check that only the earlier side reaches a prompt.

**Citations are enforced by code.** Every context item carries an id (`news:123`,
`px:AAPL`, `filing:45`, `macro:DGS10`...). After Claude answers, `validate.py` deletes any
claim whose id was not in the context sent, any claim citing an item about a different
company, and any report left with no evidence. URLs, the agent name and the timestamp
are filled in by the program, never taken from the model.

**Claude API use** (checked against the SDK docs, `anthropic` 2.8):

- `client.beta.messages.parse(..., output_format=Model)`: structured, validated output.
- Adaptive thinking; effort per agent from `config.yaml` (medium for agents, high for the judge).
- The stop reason is read before the content. A refusal, a cut-off answer and every API
  error become one of four clear exceptions; one failing agent never stops a cycle.
- Server-side refusal fallback (`fallbacks: "default"`) is on for Opus and Sonnet: if the
  safety filter declines a request, the API retries it on the model Anthropic recommends.
  The answering model is recorded and priced at its own rate.
- The SDK retries rate limits, server errors and network errors (4 retries, backoff).
- Prompt caching: all research agents share one identical first system block. The first
  agent of a cycle runs alone and writes it to the cache; the rest read it at a discount.
  `dry-run` prints how many calls hit the cache.
- Batches API: not used yet. It is for bulk, non-urgent work and arrives with the Phase 4
  historical feature extraction.

**Cost governor** (`llm/governor.py`). Before each call it reserves an estimate; after the
call the real cost is written to `api_usage`. Reservations stop parallel calls from
jointly overshooting.

| Mode | When | Effect |
|---|---|---|
| normal | below 85% of the monthly budget and within today's share | everything as configured |
| degraded | 85% of the month spent, or today used more than its share | no sector analysts or insider agent; effort one step lower |
| stopped | monthly budget spent | no API calls; the cycle ends with "no decision", not an error |

Today's share is (budget left this month / weekdays left) x 1.5. A warning flag is raised
at 70% for the Telegram alert in Phase 6.

**The judge and the final confidence.** The judge sees every surviving report grouped by
ticker, the contrarian's objections, prices, limits, positions and each agent's
reliability weight. Code then checks each proposed trade: the ticker must be a universe
stock with current prices; shorts are refused while disabled; at least one real report
must support it; the entry must be within 5% of the last close with stop and target on
the correct sides; size is capped at the per-position limit.

```
final = 0.5 x judge + 0.3 x model + 0.2 x agreement
agreement = FOR / (FOR + AGAINST + 0.5 x NEUTRAL + 0.5)
```

FOR, AGAINST and NEUTRAL are sums of (agent weight x report confidence). The constant 0.5
is built-in doubt: one agent alone at 0.8 gives 0.62, three give 0.83, and an opposing
report pulls the figure down. Until the pattern model exists (Phase 4) its term is left
out and the other two weights are scaled up to sum to 1. Trades at or above the threshold
(0.65) and within the daily maximum are marked `proposed`; everything else is stored with
the reason it was not.

Known limits:

- **Training-data leakage.** Claude may know what happened after a past date. Runs on
  past dates are optimistic; `dry-run` says so each time.
- **No call transcripts**, so the earnings agent is told not to judge management tone.
- **8-K text is the opening of the main document only.** Earnings numbers usually sit in
  an attached press release (exhibit 99.1), which is not read yet.
- **10-K / 10-Q risk-factor changes are not compared.** Those filings appear as events
  only.
- **Agent reliability weights are all 1.0** until the learning loop (Phase 7) has outcomes.
- **Holidays are not known yet**; "fresh" news reaches back over weekends only. The Alpaca
  market calendar arrives with the scheduler (Phase 7).

## Design rules

### 1. Paper trading only

There is one trading endpoint in the code: `ALPACA_PAPER_BASE_URL` in `safety.py`. It is a
constant, not a setting. `assert_paper_only()` runs before anything else at startup and will
run again whenever a trading client is created (Phase 5). No live-trading path exists.

### 2. Point-in-time data ("what did we know, and when?")

Every ingested row has `available_at`: the earliest moment the system could have known it.
Any code that decides something "as of" time T may only read rows with `available_at <= T`.
This one rule is what keeps backtests and the `dry-run` command honest. Related choices:

- All timestamps are timezone-aware UTC. The `UTCDateTime` column type rejects naive values.
- FRED rows store the *vintage* (`realtime_start`), because macro numbers get revised.
- Earnings rows store separately when the calendar entry and when the actual figures were known.
- The universe table keeps join/leave dates, so a backtest need not assume today's index members.

### 3. Everything is attributable

`trades -> trade_ideas -> judge_decisions -> decision_cycles`, and `trade_idea_reports`
links each idea to the agent reports that supported or opposed it. So when a trade closes,
the system can say which agents were right. `prediction_outcomes` scores every report, not
just the ones that became trades.

### 4. Configuration is strict

`config.yaml` holds behaviour; `.env` holds secrets. They never mix. Config objects are
immutable and validated when loaded. Some rules are enforced there rather than trusted:

- orders never before 09:35;
- position limit x number of positions cannot exceed 100% of equity (no leverage);
- confidence blend weights must sum to 1;
- every model in use must have a price, so spend can always be computed.

### 5. Two databases, one schema

PostgreSQL in Docker, SQLite for quick local work and tests. The same migrations run on
both, and CI runs the database tests against both. A test fails if `models.py` and the
migrations ever disagree.

## Database tables

| Group | Tables |
|---|---|
| Reference and ingested data | `universe_members`, `price_bars`, `news_items`, `news_tickers`, `filings`, `insider_transactions`, `institutional_holdings`, `security_identifiers`, `earnings_events`, `macro_observations`, `ingest_state` |
| Research and decisions | `decision_cycles`, `agent_reports`, `model_versions`, `model_scores`, `judge_decisions`, `trade_ideas`, `trade_idea_reports`, `lessons` |
| Trading | `trades`, `orders`, `fills`, `exit_signals`, `equity_snapshots` |
| Learning | `prediction_outcomes`, `agent_stats`, `agent_weights` |
| Operations | `api_usage`, `job_runs`, `notifications`, `system_state` |

Notable details:

- `news_items.content_hash` + `duplicate_of_id`: the same story from two sources is stored
  once as the original and once as a pointer to it.
- `ingest_state`: a bookmark per source, so ingestion can stop and resume.
- `orders.client_order_id`: chosen by us and unique, so re-sending after a crash cannot
  create a second order.
- `api_usage`: one row per Claude call with token counts and cost; the cost governor sums it.
- `job_runs`: one row per scheduled run; the heartbeat finds jobs that did not happen.
- `system_state`: pause flag, kill-switch state, last heartbeat.

### Changing the schema

Edit `db/models.py`, then:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review the generated file in `src/papertrader/db/migrations/versions/`, run the tests, commit both.

## Planned design for later phases

### Pattern model and leakage *(planned, Phase 4)*

Claude has seen historical market outcomes in its training data, so its judgement on past
events is contaminated. Historical features therefore come from ticker- and date-masked
text or non-LLM models; training is walk-forward only with an embargo; and every backtest
report will state that backtests are optimistic and **forward paper trading is the real test**.

## Testing

- `uv run pytest` runs everything; no network, no keys.
- Database tests run on SQLite, and also on PostgreSQL when `TEST_DATABASE_URL` is set.
- CI (`.github/workflows/trading-agent.yml`) runs lint, format check, type check and tests,
  then builds the Docker images, starts all three services and waits for them to be healthy.
