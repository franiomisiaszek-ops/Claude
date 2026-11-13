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

## What exists today (Phase 1)

| Piece | File | What it does |
|---|---|---|
| Paper-only guard | `safety.py` | Hard-coded paper URL; refuses live URLs, env overrides and live-looking keys |
| Configuration | `config.py`, `config.yaml` | Validated settings. Unknown keys and out-of-range values are errors |
| Secrets | `config.py` (`Settings`), `.env` | Loaded from the environment; wrapped so they cannot be printed |
| Logging | `logging_setup.py` | One JSON object per line, UTC timestamps, secret-looking fields masked |
| Database schema | `db/models.py` | All 30 tables for every phase |
| Migrations | `db/migrations/` | Alembic; runs automatically when the app starts |
| Startup | `runtime.py` | Guard first, then config, then database |
| CLI | `cli.py` | `papertrader check`, `db upgrade`, `run`, `version` |
| Service | `scheduler/service.py` | APScheduler in New York time; only a heartbeat job so far |
| Dashboard | `dashboard/` | Read-only Streamlit status page |
| Health check | `healthcheck.py` | Docker marks the app unhealthy if the heartbeat stops |

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
| Reference and ingested data | `universe_members`, `price_bars`, `news_items`, `news_tickers`, `filings`, `insider_transactions`, `institutional_holdings`, `earnings_events`, `macro_observations`, `ingest_state` |
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

### Claude API *(planned, Phase 3)*

- Official `anthropic` SDK; every agent returns a Pydantic-validated structured output.
- Adaptive thinking, effort set per agent in `config.yaml`.
- Stable system prompts are cached; backfills use the Batches API (half price).
- **Cost governor**: before each call it checks month-to-date and day-to-date spend from
  `api_usage`. At 70% of the budget it warns, at 85% it runs fewer agents at lower effort,
  at 100% it stops calling the API. It degrades; it does not crash.

### Final confidence *(planned, Phase 3)*

```
final = 0.5 x judge confidence + 0.3 x calibrated model probability + 0.2 x agent agreement
```

Weights are in `config.yaml` (`judge.blend`). Only trades with `final >= 0.65` are executed.

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
