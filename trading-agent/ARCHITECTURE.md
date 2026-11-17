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

## What exists today (Phases 1-5)

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
| Pattern model | `model/` | Dataset, walk-forward training, calibration, backtest, report, live scores |
| Trading | `trading/` | Paper broker adapter, risk rules, orders, fills, exits, kill switch |
| Notifications | `notify/` | Outbox table and message texts |

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
- Verified against the real API on 2026-11-14: 16 calls, no errors, structured outputs
  valid, cache read on every agent call after the first.
- Batches API: used for the bulk labelling of masked historical headlines (Phase 4).

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

## Pattern model (Phase 4)

```
model/text.py       masking; keyword event types and word-list sentiment (no LLM)
model/features.py   the feature builder, shared by training and live scoring
model/dataset.py    reads prices and events from the database; Parquet output
model/train.py      LightGBM, walk-forward folds with embargo, calibration, metrics
model/backtest.py   position simulation with costs; SPY and random-pick baselines
model/report.py     Markdown + HTML report, disclaimer and automatic warnings
model/registry.py   model versions and the promotion rule
model/score.py      today's probabilities for the judge
model/llm_text.py   optional Claude labels for MASKED headlines (Batches API)
```

**What one row is.** One stock on one trading day on which something happened (news, an
8-K, an earnings release, an insider trade) between the previous decision time and that
day's decision time (08:20 New York). Target: does the stock beat SPY over the next five
trading days.

**Timing, where leakage hides.**

```
events up to 08:20 on day D   ->  features, with prices up to the close of D-1
enter at the OPEN of day D    ->  hold h trading days
exit at the CLOSE of D+h-1    ->  label = return minus SPY's over the same window
```

The overnight gap into day D is deliberately not a feature: at 08:20 the system does not
know it. Tests rewrite every price from an entry day onwards and check that no feature
changes, and check that the rows built live "this morning" equal the rows history shows
for that day afterwards.

**Features.** Momentum over 1/5/20/60 days (raw and relative to SPY), volatility, volume
against its average, distance from the 52-week high and low, market regime (SPY trend
and volatility), sector, day of week, and event features: news counts, keyword event
types, word-list sentiment, earnings and surprise where known, 8-K item flags, insider
buying and selling, and Claude's labels where they exist.

**Validation.** Walk-forward only: the days are cut into seven blocks; the model is
tested on blocks two to seven, each time trained on everything earlier minus an embargo
of ten trading days so no label overlaps the test block. Nothing is shuffled.

**Calibration.** Raw scores are mapped to probabilities using scores the same kind of
model produced for days it had not seen (a walk-forward inside the training window).
Isotonic regression is used when there are at least 1,500 such examples, otherwise the
smoother Platt curve. With few examples the probability is also pulled toward 50%
(by rows / (rows + 300)), so a model built on little data cannot sound confident.

**Backtest.** Each day buy up to three candidates with probability at least 0.55, 10% of
the account each, at most ten positions, hold five days; ten basis points per side for
commission and slippage. Compared with SPY buy-and-hold over the same days and with 200
random selections of the same number of stocks from the same candidates on the same
days. Reported: hit rate, information coefficient, Brier score, AUC, calibration table,
Sharpe, maximum drawdown, turnover.

**The report polices itself.** It opens with the disclaimer (backtests flatter, leakage
through Claude, survivorship bias, forward paper trading is the real test) and adds
warnings automatically: too little data; results too good to be true (daily IC above
0.08, top-decile hit rate above 62%, Sharpe above 2); no better than random picks; no
ranking skill; probabilities no better than the base rate.

**Promotion.** A new model becomes active only if it has positive IC on the latest unseen
window, its probabilities beat the base rate, and it beats the active model on that
window (the active model is only tested on days after its own training ended). A first
model with no measurable skill is not promoted either: no model is the honest state.

**Masked text features (optional).** `model text-submit` sends headlines to Claude through
the Message Batches API (half price, cheapest model, cached instructions), with company
names, tickers, dates, years and quarters replaced by placeholders, and asks only what
the text says. `model text-collect` stores the answers and their cost. Ids the model
invents are ignored; out-of-range numbers are pulled into range.

Known limits:

- **Masking is partial.** A famous product or a unique deal size can still identify an
  event. Results that depend on Claude's labels are optimistic.
- **Survivorship bias** and **adjusted prices**, as described under ingestion.
- **Daily bars only.** Entry is assumed at the official open and exit at the close; real
  fills at 09:35 will differ, and a stop or target inside the day is not simulated.
- **No historical earnings surprise** on the free Finnhub plan, so that feature is
  mostly empty in history.
- **The backtest rule is simpler than the live system**: no sector limit, no judge, no
  agents. It tests the model alone.

## Trading engine (Phase 5)

```
trading/broker.py   the Broker interface and the alpaca-py adapter (paper only)
trading/risk.py     limits for new positions (pure functions)
trading/engine.py   orders -> fills -> trades -> exits; kill switch; reconcile
trading/thesis.py   bad news on a held stock -> news agent + judge -> keep or sell
notify/outbox.py    every message is queued in the database first
notify/messages.py  message texts
```

**Paper only, three times over.** `assert_paper_only()` runs before the client exists;
the client is created with `paper=True`; and the URL the client actually ended up with is
checked again. A test makes the SDK pretend to pick the live host and expects a refusal.

**Orders.** Each approved idea becomes one bracket order: a limit buy (at most 0.3% above
the last trade and never above the judge's entry zone) with a take-profit limit and a
stop-loss stop attached. Time in force is GTC, because Alpaca gives the two exit legs the
entry's time in force and "day" would cancel the protection at the close. Unfilled entries
are cancelled by the end-of-day reconcile. The client order id is derived from the idea
(`pt-i<idea>-entry`), so a repeated run cannot place a second order.

**Risk checks** run on the whole list in rank order, counting orders placed earlier in the
same run: no second position in a stock; at most 10 positions; size = the judge's
suggestion capped at 10% of equity; sector exposure (held + ordered) capped at 30%; only
cash not already committed (no leverage); price must still be inside the entry zone and
above the stop; target and stop must be at least 0.2% away; shorts refused while disabled.
A refused idea is stored with its reason.

**From fills to trades.** `sync()` refreshes every unfinished order (parents with their
legs). An entry fill opens a `Trade` with the SPY price at that moment and the time stop:
15 minutes before the close of the horizon's last trading day, counting the entry day as
day one and using Alpaca's calendar (holidays and half days included). A take-profit or
stop-loss fill closes it as `target` or `stop`; an exit order fill closes it with the
trigger recorded when the exit was placed. P&L, SPY's move and the excess return are stored,
and the trade-closed message names which agents were right (supporters if the trade beat
SPY, opposers if not). Fills already final when first recorded are processed too.

**Exits Alpaca cannot know about.** Time stop, thesis break, manual close: the open legs
are cancelled, then a market order is sent (retried briefly, because right after a cancel
the shares can still be held). If it keeps failing an alert is queued.

**Thesis break.** The monitor fetches fresh news for held stocks. A story about a held
stock since entry (or the last review) with a clearly negative word-list score starts a
review: the news agent writes a fresh report on the new items, then the judge sees the
original rationale, the reports behind the trade, the position and that report, and
answers sell/keep with a confidence. Only "sell" at 60% or more exits. Each position is
reviewed at most once per 6 hours. A failed review never sells.

**Kill switch.** Day change = equity / previous close equity - 1. At -3%: trading is paused,
our unfilled entries are cancelled, open positions keep their stops, one alert is queued.
It stays paused until resumed by hand.

**Reconcile.** Each monitor pass compares Alpaca's positions with open trades and raises
one alert per day for a position the system does not manage, or a trade with no position.
Every pass records an equity snapshot; the end-of-day reconcile records the closing one
and writes the summary: day P&L, P&L since start against SPY, win rate (test trades
excluded), open positions.

**Outbox.** The engine never talks to Telegram. It writes `notifications` rows with a dedup
key (one message per event, however often a step runs). Phase 6 sends them; until then the
commands print them.

Known limits:

- **Polling, not streaming.** Fills are picked up on the next monitor pass (every 10
  minutes), so a message can be up to 10 minutes late. Recorded fill times are Alpaca's.
- **One fill row per order**, with the average price, rather than one per execution.
- **Real-time prices come from the IEX feed** (the free plan), one exchange only; the limit
  price is based on it.
- **Shorts** are coded symmetrically but untested against the paper account, and off.
- **No scheduler yet**: until Phase 7, orders, monitoring and the reconcile only happen
  when a command is run. Bracket stops and targets work regardless.

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

## Testing

- `uv run pytest` runs everything; no network, no keys.
- Database tests run on SQLite, and also on PostgreSQL when `TEST_DATABASE_URL` is set.
- CI (`.github/workflows/trading-agent.yml`) runs lint, format check, type check and tests,
  then builds the Docker images, starts all three services and waits for them to be healthy.
