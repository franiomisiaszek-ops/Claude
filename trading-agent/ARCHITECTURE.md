# Architecture

This document explains how the system is put together and why. It is updated at the end
of every phase. Sections marked *(planned)* describe the design for phases not built yet.

## The daily flow

```
06:30  ingest      news, filings, earnings, macro, prices  ->  database
07:30  agents      ~18 Claude research agents, in parallel ->  agent_reports
08:20  model+judge pattern-model scores, then the judge    ->  judge_decisions, trade_ideas
08:45  notify      Telegram morning brief
08:55  hub         Top 3 ideas of A, B and C with the Avanza equivalent
09:35  trading     risk checks, bracket orders (PAPER)     ->  orders, fills, trades
 ...   monitor     every 10 minutes: exits, kill switch
16:15  reconcile   end-of-day summary                      ->  equity_snapshots
17:00  learning    score past predictions                  ->  prediction_outcomes, agent_stats
Sat    learning    re-weight agents, retrain model, weekly report
```

All times are New York time on trading days. Each step reads from and writes to the
database, so steps are independent: one can fail and be re-run without redoing the others.

## What exists today (Phases 1-8, experiment X1-X6)

| Piece | File | What it does |
|---|---|---|
| Paper-only guard | `safety.py` | Hard-coded paper URL; refuses live URLs, env overrides and live-looking keys |
| Configuration | `config.py`, `config.yaml` | Validated settings. Unknown keys and out-of-range values are errors |
| Secrets | `config.py` (`Settings`), `.env` | Loaded from the environment; wrapped so they cannot be printed |
| Logging | `logging_setup.py` | One JSON object per line, UTC timestamps, secret-looking fields masked |
| Database schema | `db/models.py` | All 31 tables for every phase |
| Migrations | `db/migrations/` | Alembic; runs automatically when the app starts |
| Startup | `runtime.py` | Guard first, then config, then database |
| CLI | `cli.py` | `papertrader check`, `db`, `run`, `dry-run`, `ingest`, `model`, `trade`, `telegram`, `jobs` |
| Service | `scheduler/service.py` | APScheduler in New York time: the daily schedule, watchdog, heartbeat, Telegram sender, bot |
| Jobs | `scheduler/jobs.py` | Every scheduled job, recorded in `job_runs`; alerts on failure; the watchdog |
| Learning | `learning/` | Scores past agent calls, per-agent accuracy, weights, weekly lessons |
| Web app | `web/`, `dashboard/data.py` | The iPhone web app (X5): login, read-only pages for A, B and C, three confirmed buttons |
| Backups | `scripts/`, `backup` service | Nightly database + model backup, restore script |
| Health check | `healthcheck.py` | Docker marks the app unhealthy if the heartbeat stops |
| Ingestion | `ingest/` | Loads and refreshes every data source (see below) |
| Claude access | `llm/` | One client, cost per call, the budget governor |
| Research agents | `agents/` | Point-in-time snapshot, 8 agent types, citation checks |
| Judge | `judge/` | Weighs reports, proposes trades; code checks and scores them |
| Research cycle | `research/` | Runs agents, contrarian and judge; `dry-run` output |
| Pattern model | `model/` | Dataset, walk-forward training, calibration, backtest, report, live scores |
| Trading | `trading/` | Paper broker adapter, risk rules, orders, fills, exits, kill switch |
| Notifications | `notify/` | Outbox, message texts, reports, Telegram sender, command bot |

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
- **Agent reliability weights start at 1.0** and move only once the learning loop has at
  least 10 resolved calls per agent (Phase 7).
- **Holidays**: the scheduled run reaches back to the previous trading day's run (from the
  Alpaca calendar), so news over a weekend or a holiday is not missed. A `dry-run` still
  uses the simpler rule (26 hours, three days more on Mondays).

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
key (one message per event, however often a step runs). The Telegram sender (below) sends them.

Known limits:

- **Polling, not streaming.** Fills are picked up on the next monitor pass (every 10
  minutes), so a message can be up to 10 minutes late. Recorded fill times are Alpaca's.
- **One fill row per order**, with the average price, rather than one per execution.
- **Real-time prices come from the IEX feed** (the free plan), one exchange only; the limit
  price is based on it.
- **Shorts** are coded symmetrically but untested against the paper account, and off.
- **The app must be running** for time stops, thesis checks and the reconcile. Bracket
  stops and targets sit at Alpaca and work regardless.

## Telegram (Phase 6)

```
notify/telegram.py  Bot API client (plain HTTPS) + flush_outbox()
notify/bot.py       CommandBot: reads commands with long polling, answers them
notify/reports.py   morning brief, weekly summary, /status, /pnl, /positions texts
notify/topideas.py  08:55 hub message: top 3 ideas of A, B, C + Avanza equivalent (no orders)
notify/alerts.py    budget alerts, job failed / job missed alerts
notify/service.py   wiring for the long-running app: sender job + bot thread
trading/control.py  pause / resume, shared by engine, CLI and bot
```

**Sending.** `papertrader run` adds an interval job (every `telegram.outbox_interval_seconds`)
that sends pending `notifications` rows oldest first, as HTML with the first line in bold,
split at line breaks under Telegram's 4096-character limit.

- A temporary problem (network, rate limit 429, Telegram 5xx) stops the run so the order is
  kept; the row's `attempts` goes up and after `max_attempts` it is marked failed.
- A problem with one message (400, e.g. bad text) marks that message failed and goes on.
- A setup problem (401 wrong token, chat not found, bot blocked) leaves every message pending
  and logs a warning: nothing is lost while you fix `.env`.
- Rows older than `max_age_hours` are marked failed ("too old to send") instead of arriving
  a day late.

**The bot.** A daemon thread long-polls `getUpdates` (`poll_timeout_seconds`), storing the
update offset in `system_state` so a restart never answers a command twice. Only
`TELEGRAM_CHAT_ID` is answered; other chats get no reply and raise one alert per chat per
day. Commands older than 15 minutes are skipped. A failing command answers with its error
name; polling errors back off from 5 s to 5 minutes and never stop the app. `/positions`
and `/pnl` ask the paper account (created lazily) and fall back to the database. The bot
replies directly; everything else goes through the outbox.

**Secrets.** The token is part of every Bot API URL, so the client never puts URLs in its
errors, and the log filter masks `/bot<token>` in the HTTP libraries' log lines.

**Budget alerts.** Each research run checks the governor and queues: a warning once a month
at 70%, a "budget-saving mode" alert once a day from 85%, a "budget used up" alert once a
day at 100%.

Known limits:

- **Long polling, one process.** Running `papertrader telegram listen` while the app container
  runs makes Telegram refuse one of them ("conflict"); the app just retries later.

## Schedule and learning loop (Phase 7)

```
scheduler/service.py   APScheduler (New York time): cron triggers from config.yaml
scheduler/jobs.py      Jobs: one method per job, the runner, the watchdog
learning/outcomes.py   daily: score agent calls whose horizon has ended
learning/stats.py      per-agent accuracy, overall / per sector / per event type
learning/weights.py    weekly: reliability weights for the judge
learning/lessons.py    weekly: the judge's lessons from resolved outcomes
```

**The runner.** `Jobs.run(name)` inserts a `job_runs` row (job, scheduled slot, started),
runs the job and records the result and a summary in `details`. The table's unique
(job, slot) key means a slot can only run once, even if two copies of the app are running.
A failure is logged, recorded and queued as a Telegram alert; it never stops the
scheduler. Trading-day jobs skip themselves on market holidays (recorded as skipped);
cron fires them Monday to Friday. `papertrader jobs run NAME` uses the same runner with a
"manual" slot (the time it was run), also on closed days.

**The market calendar** comes from Alpaca (holidays, half-day closes), cached per day,
21 days at a time. Without Alpaca keys, Monday to Friday 09:30-16:00 is assumed.

**The daily run is two jobs.** `research_agents` (07:30) runs the agents and the
contrarian and stores their reports; the cycle is left `running` with
`notes.stage = "agents_done"`. `judge` (08:20) picks up today's waiting cycle, rebuilds the
same point-in-time snapshot (same `as_of`, same fresh-news window), scores the pattern
model at 08:20 (its training decision time), and runs the judge with the open positions
and the latest lessons. A morning with no reports still stores an empty decision, so the
brief can say "no trades". Dry runs and thesis checks still run everything in one call.

**Orders** (09:35): first the judge's `exits` (held, non-test positions only) are sold with
a market order recorded as a thesis break; then today's approved ideas are placed. The
order, monitor and reconcile jobs share one lock, so they never act on the account at the
same moment. The monitor runs every 10 minutes from 09:35 until Alpaca's close (13:00 on
half days).

**The watchdog** (every 5 minutes) lists the jobs that should have started today, past
their time plus `heartbeat_grace_minutes`, and finds those with no `job_runs` row. Each gets
a `missed` row (so it is reported once) and one alert lists them all. Missed jobs are not
run late automatically: most only make sense at their time. While the market is open it
also checks that the monitor ran in the last two intervals plus the grace period (at most
one alert an hour). At startup the app compares the last heartbeat with the time and
reports how long it was off.

**Scoring a call** (17:00, after refreshing the day's prices):

    entry  = open of the first trading day whose 08:20 is at or after the call
    exit   = close `horizon_days` trading days later, counting the entry day
    excess = stock return - SPY return over the same span
    hit    = excess > 0 for long, < 0 for short; neutral calls have none
    brier  = (confidence - hit)^2

This is the pattern model's label rule, so both learn from one yardstick. SPY's bars are
the calendar. Calls on SPY itself are skipped. Only live cycles count (daily, thesis
checks, and dry runs started within 36 hours of their `as_of`): a replay of the past is
contaminated by what Claude already knows. A call whose stock has no price on the entry
or exit day stays unscored and is dropped after `resolve_max_age_days`.

**Stats** (`agent_stats`) are recomputed from scratch each evening for 30/90/365-day
windows: per agent overall (`sector = ""`, `event_type = ""`), per GICS sector of the stock
(the universe table) and per event type. Only directional calls count; the average excess
return is signed by the call's direction.

**Weights** (Saturday): `1 + scale x (hit rate - 0.5) x n / (n + prior)`, clipped to
`[weight_min, weight_max]`, from the overall 90-day row; 1.0 below `weight_min_calls`. Each
weight is a new `agent_weights` row with its basis, effective from that moment, so a
decision can always be traced to the weights it used. Hit rate rather than Brier drives the
weight because the judge needs "how often is this agent right"; the Brier score is stored
with it.

**Lessons** (Saturday): the material is aggregated hit rates (per agent, event type,
sector, confidence band), the 60 calls with the largest moves, the closed trades with which
agents were right, and last week's lessons. One structured Claude call (judge model,
medium effort, `purpose = "lessons"`, counted in the budget) returns a summary and lists of
what worked, what failed and advice. The rendered text is stored in `lessons` and the judge
receives the newest one younger than `lessons_max_age_days`.

**Weekly job** steps run independently (one failing does not stop the others): collect
headline labels, score outcomes, weights, retrain (promotion rules from Phase 4), lessons,
then the weekly summary with the weight changes, the retrain result and the lessons.

**Headline labels.** The pattern model was trained with Claude's masked headline labels.
To keep live scoring consistent, the ingest job sends new headlines (up to
`text_batch_limit` a day, only in normal budget mode) to the Batches API, and the judge,
the evening job and the weekly job collect finished batches.

Known limits:

- **One process.** Jobs run in the app container's threads. If the computer sleeps, jobs
  are missed and reported; nothing catches up by itself.
- **Few outcomes for weeks.** A 5-day call is scored 5 trading days later; weights and
  lessons need several weeks of calls before they carry information. Small samples are
  shrunk toward "no track record" on purpose.
- **Lessons are written by Claude** from the figures given; the prompt forbids outside
  knowledge, but the text is advice to the judge, not a tested rule.

## Dashboard and deployment (Phase 8)

```
dashboard/data.py     plain query functions returning pandas frames (now used by the web app, X5)
scripts/backup.sh     nightly backup loop (runs in the backup container)
scripts/restore.sh    restore one backup, all or nothing
docs/WINDOWS.md       the beginner's guide for running it on the Windows PC
```

**Dashboard.** Replaced in X5 by the web app (below); its query functions in `data.py`
stayed and feed the web app's pages.

**Backups.** A fourth container runs the `postgres:16-alpine` image, so `pg_dump` always
matches the server. Once a day after 05:00 UTC it writes `papertrader-<stamp>.dump`
(custom format, written to a `.part` file and renamed, so a failed dump never looks like a
backup) and `models-<stamp>.tgz` (the `pm-*` model folders; training datasets are left
out because they are rebuilt from the database), then keeps the newest `BACKUP_KEEP`
(default 7). `BACKUP_DIR` in `.env` can point the folder at OneDrive for an off-site copy.
`restore.sh` asks for YES, runs `pg_restore --clean --if-exists --single-transaction` (all
or nothing) and puts the models back. The scripts are forced to Unix line endings by
`.gitattributes`, and the service strips carriage returns before running, in case a
Windows checkout added them.

**Watchdog additions.** The app mounts the backups folder read-only and alerts (once a
day) when the newest dump is older than 36 hours, or when none exists after 48 hours of
running. Once an hour it compares its clock with Alpaca's and alerts beyond 60 seconds
(Docker's clock can drift after the PC sleeps); `papertrader check` shows the same.

**Long-running hygiene.** Container logs are capped at 5 x 10 MB each; weekly retraining
deletes all but the newest 3 training datasets.

Known limits:

- **One PC.** If it is off, asleep or signed out, jobs are missed (and reported once it is
  back). Stops and targets still work at Alpaca.
- **Backups on the same disk** protect against mistakes, not a dead disk, unless
  `BACKUP_DIR` points at OneDrive or they are copied elsewhere.
- **Restore is manual** and replaces everything after the backup.

## The three-way experiment (X1)

Three systems run side by side, each on its own Alpaca paper account
([docs/EXPERIMENT.md](docs/EXPERIMENT.md) is the user guide):

```
                         one PostgreSQL database
  ┌──────────────────────────────────────────────────────────────────────┐
  │ public: market data (prices, news, filings, ...)  + A's own tables   │
  │ sys_b:  B's own tables       sys_c: C's own tables                   │
  └──────────────────────────────────────────────────────────────────────┘
      ▲ writes market data       ▲ reads market data      ▲ reads market data
  app (A, frozen image)       app-b (B)                app-c (C, from X4)
      │ own paper account         │ own paper account        │ own paper account
      └──────── outboxes ─────────┴──────────────────────────┘
                                  ▼
                hub: the one Telegram bot, tags [A] [B] [C]
```

| Piece | File | What it does |
|---|---|---|
| Systems | `systems.py` | The names A, B, C; which tables are shared market data and which every system has its own copy of; the frozen revision |
| Per-system connections | `db/session.py` (`make_system_engine`) | B's PostgreSQL connections use `search_path=sys_b,public`: unqualified table names find B's own tables first and the shared market tables otherwise. On SQLite, B gets its own file with A's attached |
| Provisioning | `db/provision.py` | Creates a system's own tables in its schema (`papertrader --system B db provision`, also done at start) and verifies them, so B can never fall through to A's decisions |
| Keys and guard | `config.py`, `safety.py`, `runtime.py` | `ALPACA_B_*` / `ALPACA_C_*`; every system must be on the paper endpoint and no two may share a key |
| Start-up order | `runtime.py` (`wait_for_shared_schema`) | Only A creates the shared tables; B, C and the hub wait for it, so they never race on a new database |
| B reuses A's research | `research/reuse.py` | At 08:25 B copies A's finished agent reports into its own tables and runs its own judge. Allowed only while the agents' settings and prompts hash to the frozen fingerprint |
| Per-system jobs | `scheduler/jobs.py` (`SYSTEM_JOBS`) | A runs everything. B runs judge, brief, orders, monitor, reconcile, scoring and weekly, but never loads market data and never retrains the pattern model. C's jobs arrive in X4 |
| Hub | `hub.py`, `scheduler/service.py` (`run_hub`) | A Telegram bot can be read by only one program, so the systems only queue messages; the hub sends them all, tagged, and answers `/pnl B`-style commands |
| Freeze tests | `tests/systems/test_freeze.py` | Fail if a migration is added, a shared table or column changes, or `config/a.yaml` changes |

The freeze: A runs the Docker image `papertrader:a-frozen`, built once from commit
`e54f521`, with `config/a.yaml`. Everything new goes into B or C. A shared table cannot
gain a column (A's frozen code would not know it), so B's and C's new data gets new
tables in their own schemas.

## System B's decision layer (X2)

`b/layer.py` plugs into the judge stage through a small protocol (`JudgeLayer` in
`research/cycle.py`): `prepare` before the judge, `apply` after it, `record` next to the
stored decision. A's frozen code never sees it.

```
A's reports (copied) --prepare--> judge (Opus) --apply--> B's trade ideas --> orders
      | 4 pruning                                 | 1 calibration + abstain
      | 5 insiders, 6 sector, 8/9 paid signals    | 10 meta-label, 2 ATR/size/SPY filter
      |   (extra context, facts only)             | 7 cost band, 3 review (remove/shrink only)
                                                  +--> b_decision_log: A's answer vs B's, step by step
```

| Piece | File | What it does |
|---|---|---|
| Switches | `config.py` (`DecisionConfig`), `config/b.yaml` | Ten switches plus the Fable test; all off = A's behaviour |
| B-only tables | `b/models.py` | Own `MetaData` (not the shared `Base`), provisioned into B's schema: decision log, signals, calibrations, Fable reports, backtests. The freeze tests are untouched |
| Past ideas and their outcome | `b/outcomes.py` | Ideas from B's log and from A's stored decisions (read-only), scored like agent calls: open after the decision to the close `horizon` days later, against SPY or the sector fund |
| Calibration, abstaining | `b/calibrate.py` | Isotonic regression on the older 70% of resolved ideas, split-conformal level on the newest 30%; inactive below `min_ideas` |
| Pruning | `b/pruning.py` | Shrunk hit rate per agent from A's scored calls |
| Review | `b/veto.py`, `b/prompts.py` | One structured call; verdicts for unknown tickers ignored; fails open. B's prompts live apart from `agents/prompts.py`, which is part of the reuse fingerprint |
| Signals | `b/signals.py`, `b/paid.py`, `b/daily.py` | Insider clusters from Form 4 (point-in-time), MarketData.app and FMP adapters (key-gated, strict format checks), the 08:00 job |
| Backtest | `b/backtest.py` | Replays the insider rule without look-ahead; month-block bootstrap; verdict rule fixed in advance |
| Meta-labelling | `b/meta.py` | Regularised logistic regression; used only if it beats the blend and the base rate on the newest ideas |
| The Fable test | `b/daily.py` (`run_fable`, `score_shadow`, `fable_comparison`) | Chosen agents re-run on Fable from B's copy of the day, never traded; own budget through `CostGovernor(only=...)`, excluded from B's main budget |

## System B's intraday scanner (X2b)

`b/scanner.py`, run by the `intraday` job every `interval_minutes` during market hours
(a repeating job like the monitor: `JobSpec.every`).

```
live quotes (IEX snapshot, whole universe) --rules--> candidates (free)
   --Haiku screen--> picks --Sonnet decision--> code checks --> b_intraday_plans + Telegram alert
live mode:   after the /skip window: price still in the zone? limits? day-trade rule?
             --> intraday DecisionCycle + TradeIdea --> TradingEngine.place_orders (same risk checks)
             time stop from the expected holding time --> TradingEngine.exit_trade(TIME)
shadow mode: nothing placed; after the close, simulate() on the full market's 1-minute bars
both:        feed_gap_bps = the IEX price used vs the full market's at that minute
```

| Piece | File | What it does |
|---|---|---|
| Live data | `b/live.py` | Alpaca snapshots, minute bars (IEX live, SIP for scoring) and headlines; never written to the shared tables |
| Rules, plans, scoring | `b/scanner.py` | Day facts from the shared daily bars, the three rules, budget caps from `api_usage`, plan checks, sizing, `add_market_minutes`, `place_due`, `manage_open`, `simulate`, `score_plans`, `results` |
| Plans | `b/models.py` (`IntradayPlan`) | One row per plan from alert to result; real trades tagged through `idea_id` / `trade_id` |
| /skip | `hub.py` | Handled before the system argument is read (A and C are also tickers) |
| Day-trade rule | `trading/broker.py` (`Account.daytrade_count`) | Read from Alpaca; no new intraday trade with 3 day trades on an account under $25,000 |

## System B's post-mortems (X2c)

`b/postmortem.py`, run by the `postmortems` job at 07:00 New York for the previous
trading day, and used by `b/layer.py` at the judge.

```
yesterday: closed trades, big moves in held stocks, passed-on ideas that moved (decision log)
   --select_cases (at most 10, by importance)--> Haiku write-up per case (sources marked
   "known then" / "after"; causes with likelihoods; decision quality without hindsight)
   --> b_postmortems (+ evidence level counted in code) --> Sonnet digest --> Telegram
judge time: similar() past cases + explicit differences --> one comparability call
   --> adjustment() capped by evidence, only in the lesson's direction, never upward
       across the bar, never size or risk --> b_lesson_uses --> monthly evaluate()
```

| Piece | What it does |
|---|---|
| `select_cases` | Picks closed trades, big moves (vs the sector fund, in daily ranges), missed chances and dodged bullets from B's own records; skips cases already written |
| `sources`, `render_case`, `write_case` | Shared news, filings and earnings around the case, each marked known-then or after; made-up source ids are dropped; a market/sector/noise cause is always present |
| `similarity`, `similar`, `evidence` | Same ticker, kind of news, sector, market trend, volatility; differences listed in words; anecdote / emerging / supported from counts |
| `DecisionLayer._lessons` | Only for trades B would place; the reviewer judges comparability; nudges recorded in `b_lesson_uses`; ranking ignores upward nudges |
| `evaluate`, `prune` | Brier score with vs without on resolved ideas, losers vs winners stopped; the three-month rule; old and never-comparable cases retired |

## Measurement (X3)

`experiment/`, read-only over every system's own tables and the shared prices; the rules
are pre-registered in [docs/PREREGISTRATION.md](docs/PREREGISTRATION.md).

| Piece | File | What it does |
|---|---|---|
| Start date | `experiment/measure.py` (`get_start`, `set_start`) | Kept in A's `system_state` (`experiment.start`), like the hub's Telegram offset |
| Loading | `measure.load` | End-of-day equity, Claude spend per day, and every judged idea (from the stored decisions; the scanner's own are left out) |
| Shadow scoring | `measure.signal` | Ideas scored at 1/5/10 days against their sector fund with `b/outcomes.resolve`; daily rank IC, Brier, hit rate |
| Money | `measure.net_returns`, `spy_returns` | Daily net return after Claude and data costs; SPY's daily total return |
| Statistics | `experiment/stats.py` | Spearman, moving-block bootstrap (5-day blocks, fixed seed), Newey-West (5 lags), Holm |
| Hypotheses and verdict | `measure.hypotheses`, `measure.verdict` | H1-H3 paired by date; the pre-registered decision rule; the $1,000 cost rule |
| Switch review | `experiment/switches.py` | B's decision log: trades each switch removed or added, scored at 5 days |
| Reports | `experiment/weekly.py`, the hub's `weekly_comparison` job, `papertrader experiment report`, the dashboard page | One text and one set of numbers everywhere |

## System C (X4)

`c/`: rules first, Claude only to reduce risk. Its own tables (`c/models.py`, own
MetaData, schema `sys_c`): prices, weekly plans, orders, daily-trade plans, backtest runs.

| Piece | File | What it does |
|---|---|---|
| Rules | `c/strategy.py` | Eligibility (most of 1/3/6/12-month returns positive, above the 200-day average, optional dual momentum), momentum ranking, inverse-volatility or risk-parity weights, per-fund and group caps (excess stays in cash), the 10% volatility target (never above 1x), drawdown states, drift-band orders |
| Data | `c/data.py` | Alpaca daily bars (the live window, refetched daily), Tiingo history and the FRED T-bill rate for the backtest; the T-bill fund's history chained back through BIL and the rate |
| Backtest | `c/backtest.py` | Friday signals, Monday-open trades, costs, drift band; 12-setting grid chosen on 2004-2015, tested once on 2016+; upgrades and the daily trades recommended only if they help in both periods; SPY and 60/40 |
| Daily trades | `c/sleeve.py` | Breakout and dip rules in uptrends, ATR stop and target, time limit; used by the backtest and the shadow run |
| Claude | `c/review.py` | Weekly review that may only reduce or skip buys; daily news watch that only alerts |
| Live | `c/live.py`, jobs `c_data`, `c_plan`, `c_rebalance`, `c_watch`, `c_sleeve`, and the monitor | The weekly plan with the Avanza list, limit orders, emergency stops, the 15% flat rule, the review's effect vs the shadow C |
| Orders | `trading/broker.py` (`submit_limit`, `submit_stop`) | Plain day limit orders and resting GTC stops |

## The web app (X5)

`web/`: a small Starlette server, rendered on the server with Jinja2 templates; the only
JavaScript (`static/app.js`) draws the charts at the screen's real width with a crosshair
and tooltip, reloads live pages and registers the service worker. No outside libraries or
CDNs, so the content security policy can forbid everything not from the app itself.

| Piece | File | What it does |
|---|---|---|
| Server | `web/app.py` | Routes, the page wrapper (login check, read-only views, errors shown without stack traces), security headers, allowed host names, the confirm-then-act flow, the manifest and service worker |
| Login | `web/auth.py` | scrypt password hash in `<data>/web_auth.json` (mode 600), signed session cookies (HMAC-SHA256, 30 days), CSRF tokens tied to the session, the growing lockout |
| Pages | `web/data.py` | Per-page queries over each system's read-only connection (A, B, C), reusing `dashboard/data.py` and `experiment/measure.py` |
| Live numbers | `web/live.py` + the hub's `web_live` job | The hub, which holds the paper keys, stores each account's equity and positions every 2 minutes in market hours (`web.live` in each system's state table); the web app only reads them |
| Buttons | `web/actions.py` | Pause, resume, skip B's intraday plan: one writable connection per action, a Telegram message, a line in `<data>/web_audit.jsonl` |
| Look | `web/templates/`, `web/static/` | Phone-first, light and dark from the device, the validated categorical palette (A blue, B orange, C aqua, SPY yellow), a table under every chart |

Security choices: the container gets only `DATABASE_URL` (no `.env`); it listens on
`127.0.0.1:8501` and Tailscale Serve brings it to the phone with HTTPS; every page needs
a session; actions need the session, a CSRF token, a same-site Origin and an explicit
confirmation; host names other than localhost and `*.ts.net` are refused (no DNS
rebinding); pages use connections opened with `default_transaction_read_only` (SQLite:
`query_only`). The service worker keeps only the style, script, icons and an offline page,
never a page with data.

## The Telegram upgrade (X6)

| Piece | File | What it does |
|---|---|---|
| Heads-ups | `notify/warnings.py` | `order_warnings` (B and C only, own schema): one row per announced order with a stable reference (`idea:12`, `exit:7:time:<day>`, `intraday:5`, `c:3:GLD:buy`), one Telegram message per group; `Gate.warn` / `decide` / `due` / `mark_sent`; the buttons (`keyboard`) and the taps (`tap`) |
| Order texts | `notify/ahead.py` | The heads-up for a buy at the open or a sell; A's information-only heads-ups and the near-stop/target warnings, written by the hub |
| Where orders wait | `trading/engine.py` (`place_orders`, `exit_trade`, `time_stops`), `b/scanner.py` (`place_due`), `c/live.py` (`warn_week`, `rebalance`) | Skipped orders are not sent; orders whose time has not come wait. Kill-switch, manual and drawdown exits are never gated |
| Schedule | jobs `warn_orders` (B), `c_warn` (C) at 09:32; `orders_due` every minute in market hours (B, C; not recorded unless it acts); the hub's `a_ahead` at A's 09:32 and the checks after each live snapshot | Nothing sleeps while waiting |
| Hub | `hub.py` | Buttons on order warnings, taps (only from your chat; the data names the system), removing stale buttons, quiet hours, app links for long reports, `/ask` |
| Sending | `notify/telegram.py` | Long texts split at line breaks ("part 2 of 3"), never cut; buttons under the last part |
| News | `notify/news.py` | Bullets: headline, why it matters (the citing agent's claim, else the stored labels), source, plain web link only |
| /ask | `notify/ask.py` | A compact summary of the systems' data and a Haiku answer from it only; purpose "ask" in B's spending, outside B's budget and the comparison |

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
  then builds the Docker images, starts the services and waits for them to be healthy.
