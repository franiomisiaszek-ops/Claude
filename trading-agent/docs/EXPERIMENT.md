# The three-way experiment

Three systems trade side by side, each on its **own Alpaca paper account** (pretend money),
from the same start date with the same starting money ($25,000 each). After at least three
months we compare their profit after all costs against simply holding SPY, and keep the
cheapest one that actually wins. If none of them beats SPY, the honest answer is: hold SPY.

| System | What it is | Claude budget |
|---|---|---|
| **A** | The original system, **frozen**: its code and settings do not change during the test. Its learning loop keeps running. | $60 / month |
| **B** | Starts every day from A's research (a copy of A's agent reports, so no extra agent cost), then decides with a better decision layer. | $30 / month |
| **C** | A weekly trend-following ETF portfolio (stocks, bonds, gold, commodities), mostly fixed rules. | $5 / month |

There is still no live-trading code anywhere. Every system refuses to start unless it
points at the paper endpoint, and they refuse to start if two systems share a key.

## Build status

| Phase | What | Status |
|---|---|---|
| X0 | Claude Code helper pool (`.claude/` folder, used while building, never by the running program) | done |
| X1 | Separate systems: own database sections, own paper accounts, own budgets; one Telegram hub | **done** |
| X2 | B's better decision layer (switches) | not started |
| X2b | B's intraday scanner (shadow mode first) | not started |
| X2c | B's post-mortems ("why did it move?") | not started |
| X3 | Measurement: the pre-registered test and the comparison page | not started |
| X4 | System C | not started |
| X5 | iPhone web app over Tailscale | not started |
| X6 | Telegram upgrade (warnings before orders, Skip button, no length limit) | not started |

Until X2 is built, B makes the same kind of decisions as A (its own judge on A's
research). That is on purpose: it checks the plumbing before B gets its own ideas.
The official start date, when all three accounts begin at $25,000, is set in X3.

## Set it up (once)

All commands are for PowerShell, in the `trading-agent` folder.

**1. Two more Alpaca paper accounts.** In the Alpaca dashboard, add two new **paper**
accounts (the account menu at the top left) with a starting balance of **$25,000** each:
one for B, one for C. Each paper account has its own API keys: open the account and
generate them on its home page. Keep A's account as it is for now; all three are set to
$25,000 together on the official start date.

**2. Put the new keys in `.env`** (Notepad), under the existing ones:

```
ALPACA_B_API_KEY=PK...
ALPACA_B_SECRET_KEY=...
ALPACA_C_API_KEY=PK...
ALPACA_C_SECRET_KEY=...
```

Paper keys start with `PK`. The names must be exactly these. A keeps using
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.

**3. Get the new code:**

```bash
git pull
```

**4. Build A's frozen image (once, never again during the experiment).** This builds
system A exactly as it was at the freeze (commit `e54f521`), in a temporary folder next
to the project:

```bash
git worktree add ..\..\papertrader-a-frozen e54f521
```

```bash
docker build -t papertrader:a-frozen ..\..\papertrader-a-frozen\trading-agent
```

```bash
git worktree remove ..\..\papertrader-a-frozen
```

**5. Start everything:**

```bash
docker compose up -d --build --remove-orphans
```

`--remove-orphans` matters this once: it removes the old single app container, so A does
not run twice.

**6. Check it:**

```bash
docker compose ps
```

`app` (system A) and `app-b` should say `healthy` within a minute or two; `hub`, `db`,
`dashboard` and `backup` should say `Up`. On Telegram, send `/status`: you get an answer
from `[A]`, and from `[B]` too within 10 minutes (the hub looks for newly set-up systems
every 10 minutes).

## Day to day

* Every Telegram message starts with `[A]`, `[B]` or `[C]`.
* Commands take a system: `/pnl B`, `/positions A`, `/pause B earnings week`, `/resume B`.
  Without one, `/status`, `/positions`, `/pnl` and `/brief` answer for every system.
  `/pause` and `/resume` always need the system.
* Logs: `docker compose logs -f app` (A), `docker compose logs -f app-b` (B),
  `docker compose logs -f hub` (Telegram).
* One system's command line: `docker compose exec app-b papertrader check`
  (B), `docker compose exec app papertrader check` (A).
* The dashboard still shows system A. The web app (X5) will show all three.
* Backups include all three systems (they are all in the one database).

## The daily pipeline (New York time)

| Time | What |
|---|---|
| 06:30 | A loads the shared market data (news, filings, prices) for everyone |
| 07:30 | A's research agents |
| 08:20 | A's pattern model and judge |
| 08:25 | B copies A's agent reports and runs its own judge on them |
| 08:45 | Morning briefs, tagged [A] and [B] |
| 09:35 | Orders (each system on its own account) |
| every 10 min | Each system watches its own positions |
| 16:15 | End-of-day reconcile |
| 17:00 / 17:10 | A, then B, score their past calls |
| Saturday | Weekly reports |

B only reuses A's reports while the agent settings and prompts are exactly the frozen
ones (it checks a fingerprint every morning). If they ever differ, B refuses and alerts
you instead of judging reports it would not have got itself.

## The freeze rules

So the comparison stays fair, A must not change during the experiment:

* Never rebuild `papertrader:a-frozen`. `docker compose up -d --build` rebuilds only B,
  C, the hub and the dashboard; A's image is left alone.
* Never edit `config/a.yaml`. (B's settings are `config/b.yaml`, C's `config/c.yaml`.
  `config.yaml` is now only used by the hub and the dashboard.)
* The shared database layout does not change: new tables for B or C go in their own
  sections. Automated tests enforce all three rules.
* If a bug has to be fixed for everyone (for example a data source changes its format),
  it is written down below and applied to all systems, including a rebuilt A image.

## Changes during the experiment

| Date | Change | Applied to |
|---|---|---|
| 2026-11-20 | A frozen at commit `e54f521` | A |

## How the data is kept apart (for the curious)

All systems use the one PostgreSQL database. The market data (prices, news, filings,
...) is loaded once, by A, and read by everyone. A's own tables stay where they always
were; B's and C's decisions, orders, trades, outcomes, messages and spend are in their
own sections (`sys_b`, `sys_c`), which B and C create for themselves on first start.
A system can read the shared market data but never another system's decisions.
Details: [ARCHITECTURE.md](../ARCHITECTURE.md), "The three-way experiment".
