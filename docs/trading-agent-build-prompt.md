# Build prompt: Multi-agent news → stock research → paper-trading system

Copy everything below the line into the new Claude Code session.

---

You are building a production-quality Python system for me, in this repository, on the branch you're given. Read this entire spec before writing code. Work in the phases below. **At the end of each phase: run the tests, commit, push, and stop to give me a short report of what works, what doesn't, and what you need from me.** Don't start the next phase until I say go.

## 0. Decisions — ask me first if any are still blank

| Decision | My answer | Default if I say "use default" |
|---|---|---|
| Hosting | ___ | Docker Compose on a small Linux VPS (Hetzner/DigitalOcean) |
| Stock universe | ___ | S&P 500 |
| Run frequency | ___ | Daily pre-market cycle + position monitoring every 10 min during market hours |
| Monthly API budget (USD) | ___ | $100, enforced in code |
| Models for agents | ___ | Judge: `claude-opus-5-5`. Research agents: configurable per agent in `config.yaml` (ask me whether to use `claude-opus-5-5` or a cheaper model such as `claude-sonnet-5-5` / `claude-haiku-5-5`) |

**Accounts and keys I'll provide** (never commit them; load from `.env`, and ship a `.env.example`): `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (paper account), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `FINNHUB_API_KEY`, `FRED_API_KEY`, and a `SEC_USER_AGENT` string for EDGAR.

## 1. Goal

1. Ingest news, SEC filings, earnings, macro data and prices.
2. Run many specialised **research agents** (Claude API) in parallel. Each one writes a structured report.
3. Score those reports with a **pattern model** trained on historical news → price-reaction data.
4. A **judge agent** checks everything, rejects weak or contradictory claims, and produces a ranked list of the highest-confidence trades plus a written summary.
5. **Paper-trade automatically** on Alpaca, with strict risk rules.
6. Send me **Telegram notifications**: the morning brief, every trade, sell signals, closed-trade outcomes, end-of-day and weekly summaries.
7. **Learn continuously**: score every past agent call against what actually happened, re-weight the agents, and retrain the pattern model.

**PAPER TRADING ONLY.** Hard-code the Alpaca paper endpoint. Add a startup guard that refuses to run if the base URL isn't the paper URL. There is no live-trading code path.

## 2. Tech stack

- Python 3.12, `uv`, `pydantic` v2, `httpx`, `asyncio`. Official `anthropic` SDK and `alpaca-py`.
- Storage: PostgreSQL (SQLite allowed for local dev) via SQLAlchemy + Alembic migrations. Parquet for bulk historical data.
- ML: `lightgbm`, `scikit-learn`, `pandas`/`polars`.
- Scheduling: APScheduler, with times in America/New_York and the Alpaca market calendar (holidays, half days).
- Dashboard: Streamlit (read-only).
- `pytest`, `ruff`, `mypy`. GitHub Actions CI running lint + tests.
- Docker + docker-compose (app, db, dashboard). Structured JSON logging.

## 3. Claude API usage rules

- Use the official `anthropic` Python SDK. Before writing API code, check the current docs and the claude-api skill. Don't write calls from memory.
- Every agent returns a **Pydantic-validated structured output** (`client.messages.parse(..., output_format=Model)`). No free-text parsing.
- Use adaptive thinking. Set `output_config.effort` per agent in config (default `medium` for research agents, `high` for the judge).
- Handle `stop_reason == "refusal"` and the other stop reasons. Use typed SDK exceptions with retry/backoff for 429/5xx.
- Use prompt caching for the static parts of the system prompts. Use the Message Batches API (50% cheaper) for historical backfills and other non-urgent bulk work.
- **Cost governor**: log the token usage of every call to the DB, compute daily and monthly spend, and degrade gracefully when close to budget (fewer agents, lower effort) instead of failing.

## 4. Data ingestion (`ingest/`)

| Source | Data |
|---|---|
| Alpaca Market Data | Daily and intraday bars, plus the **Alpaca News API** (historical back to ~2015 and live) |
| SEC EDGAR | 8-K, 10-Q, 10-K, Form 4 (insiders), 13F. Respect the rate limit and the User-Agent rule |
| Earnings | Calendar + EPS/revenue actual vs estimate (Finnhub if a key is provided) |
| FRED | Rates, CPI, unemployment, yield curve |
| Universe | Index constituents, GICS sector, market cap |

Requirements:
- Every record gets a `published_at` / `available_at` UTC timestamp.
- Deduplicate news across sources.
- Make ingestion idempotent and resumable.
- Map each item to tickers.

## 5. Research agents (`agents/`)

Each agent is a class with a system prompt, a context builder that only uses data available **before** the decision timestamp, and an output schema:

```
AgentReport: agent_name, as_of, ticker, direction (long/short/neutral),
confidence (0-1), horizon_days, thesis, evidence[{claim, source_id, url}],
risks[], catalysts[]
```

Agents (one instance per sector where marked):
1. **Macro**: Fed, rates, inflation, and their sector implications
2. **Earnings**: surprises, guidance changes, call tone
3. **SEC Filings**: material 8-Ks, risk-factor changes
4. **Sector Analyst ×11** (one per GICS sector)
5. **News Sentiment / Event**: classifies event type (M&A, FDA, lawsuit, guidance, product, management change…)
6. **Technicals**: works from computed indicators (momentum, RSI, volume spikes, gaps, 52-week levels), not chart images
7. **Insider & Institutional**: Form 4 clusters, 13F changes
8. **Contrarian / Risk (bear case)**: tries to disprove the other agents' top ideas

Requirements:
- Agents run concurrently with a concurrency limit.
- Every claim must cite a `source_id` from the provided context. Any claim without a citation is dropped.

## 6. Pattern model (`model/`) — "learn from the past"

- **Dataset**: for each historical news/filing/earnings event, compute features (event type, sentiment, surprise magnitude, pre-event momentum, volatility, volume, sector, market regime) and labels: forward 1/3/5/10-day **excess return vs SPY** and the direction.
- **Leakage warning, critical**: Claude has seen historical outcomes in its training data, so LLM judgments on past events are contaminated.
  - For historical feature extraction, use **ticker- and date-masked text** (replace company names/tickers/dates with placeholders) or a non-LLM model (e.g. FinBERT).
  - Treat LLM backtest results as optimistic. **Forward paper trading is the real test**, and the reports must say so.
- Train LightGBM with **walk-forward (time-ordered) validation only**, never random splits. Purge/embargo overlapping windows.
- Calibrate the probabilities (isotonic regression or Platt scaling) so that "80% confidence" means about 80%.
- Output a backtest report: hit rate, information coefficient, Sharpe, max drawdown, turnover, and comparison vs SPY buy-and-hold and vs a random baseline, with transaction costs/slippage assumed.
- Version models and save metrics. Only promote a new model if it beats the current one on the latest out-of-sample window.

## 7. Judge (`judge/`)

Inputs: all agent reports, the pattern-model scores, current positions, the risk limits, and each agent's historical accuracy.

The judge:
- Checks that claims are consistent and that citations exist
- Penalises disagreement and resolves conflicts
- Weights agents by their tracked reliability
- Outputs a `JudgeDecision`: ranked trades (ticker, action, confidence, entry zone, target, stop, size suggestion, horizon, rationale, dissent), positions to exit with reasons, and a market summary

Final confidence = a documented blend of the judge's confidence, the calibrated model probability and agent agreement. Only trades above a configurable threshold (default 0.65) are executed.

## 8. Trading engine (`trading/`)

- **Orders**: Alpaca paper, bracket orders (entry + take-profit + stop-loss), market or limit orders placed after 9:35 ET.
- **Default risk limits** (all configurable):
  - At most 10% of equity per position and 10 positions open
  - At most 30% in one sector
  - No leverage, no options; shorts disabled by default
  - Daily loss kill switch at −3%, which pauses trading and notifies me
- **Exit logic**: take-profit, stop-loss, time stop (horizon expired), or a thesis break (new negative news on a held ticker triggers a re-evaluation by the relevant agent plus the judge).
- **Position monitor**: every N minutes during market hours.
- **Records**: every order, fill and closed trade goes to the DB, linked to the reports and decision that caused it, so outcomes can be attributed to agents.

## 9. Telegram notifications (`notify/`)

Send via the Telegram Bot API using `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Use clean, emoji-light formatting.

| Event | When (ET) | Content |
|---|---|---|
| Morning brief | 8:45, trading days only | Top 3–5 trades: ticker, action, confidence %, entry/target/stop, a one-line reason; market summary; open positions |
| Trade executed | On fill | e.g. "BOUGHT 20 NVDA @ $142.10 · conf 82% · reason…" |
| Sell signal | Real time | Ticker and trigger (target, stop, time, or thesis break) |
| Trade closed | On fill | Exit price, P&L in $ and %, holding period, which agents were right or wrong |
| End of day | 16:15 | Day P&L, total P&L vs SPY, win rate, open positions |
| Weekly | Saturday | Agent leaderboard, model retrain result, API spend |
| Alerts | On event | Errors, kill switch, budget warnings, missed scheduled jobs |

Bot commands: `/positions`, `/pnl`, `/brief`, `/pause`, `/resume`, `/status`. Only accept commands from `TELEGRAM_CHAT_ID`.

## 10. Learning loop (`learning/`)

- **Daily**: resolve the outcomes of past predictions whose horizon has ended. Store per-agent hit rate, Brier score and average excess return, broken down by sector and event type.
- **Weekly**:
  - Update the agent reliability weights the judge uses
  - Retrain the pattern model on the new data (walk-forward); promote only if better
  - Store a "lessons" summary that the judge gets as context: patterns that worked or failed, written by the judge from the resolved outcomes

## 11. Daily schedule (ET, trading days only)

| Time | Job |
|---|---|
| 06:30 | Ingest overnight news, filings, macro |
| 07:30 | Research agents |
| 08:20 | Pattern model scoring + judge |
| 08:45 | Telegram morning brief |
| 09:35 | Place orders |
| 09:35–16:00 | Monitor every 10 min |
| 16:15 | Reconcile + EOD summary |
| 17:00 | Resolve outcomes |
| Saturday | Retrain + weekly report |

Add a heartbeat: alert me if a job doesn't run.

## 12. Dashboard (`dashboard/`, Streamlit)

Pages: equity curve vs SPY, open and closed trades, today's decision with all agent reports, the agent leaderboard, model metrics, API spend.

## 13. Phases and acceptance criteria

1. **Scaffold**: repo structure, config, `.env.example`, DB schema + migrations, logging, CI, Docker. *Done when* `docker compose up` starts and the tests pass.
2. **Ingestion**: all sources, plus a historical backfill command (configurable years, default 5). *Done when* a backfill for 20 tickers works, is idempotent and has passing tests.
3. **Agents + judge**: all agents and the judge, with a `dry-run` CLI that prints a full decision for a given date using only data available at that time. *Done when* it runs end to end on today's data, stays within budget, and the outputs validate.
4. **Pattern model**: dataset builder (with masking), walk-forward training, calibration, backtest report. *Done when* the HTML/Markdown backtest report has baselines and a leakage disclaimer.
5. **Trading engine + risk**: paper orders, the monitor, exits, the kill switch, the paper-URL guard. *Done when* a full simulated day works against the Alpaca paper account.
6. **Telegram**: all message types and commands. *Done when* I receive a test message of each type.
7. **Learning loop + scheduler**: outcome resolution, agent weights, weekly retrain, the full schedule, the heartbeat.
8. **Dashboard + deployment**: deploy docs for my hosting choice (step by step for a beginner), backups, an update procedure.
9. **Go live on paper**: run for at least 4–8 weeks before drawing any conclusions about performance.

## 14. Rules for you

- Never commit secrets. Never add live-trading capability.
- Ask before adding paid services or dependencies that cost money.
- Write tests with mocked external APIs. Keep the project's README and an `ARCHITECTURE.md` up to date.
- Be honest in reports: if results look too good, suspect leakage and say so. No guarantees of profit; this is an experiment.
- Explain things simply. I'm not an expert programmer.
