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
| X1 | Separate systems: own database sections, own paper accounts, own budgets; one Telegram hub | done |
| X2 | B's better decision layer: ten switches, extra signals, the Fable test (see below) | done |
| X2b | B's intraday scanner (see below; shadow mode first) | done |
| X2c | B's post-mortems: "why did it move?" and careful lessons (see below) | done |
| X3 | Measurement: the pre-registered test, the weekly comparison, the comparison page (see below) | done |
| X4 | System C: the weekly ETF portfolio, its 20-year backtest, the Avanza list (see below) | done (settings set from the backtest) |
| X5 | iPhone web app over Tailscale ([WEBAPP.md](WEBAPP.md)); replaces the Streamlit dashboard | **done** (needs your set-up) |
| X6 | Telegram upgrade: a heads-up with Skip / Go now before every order, no cut-off messages, news bullets, `/ask`, quiet hours (see below) | **done** |

B judges the same research as A, then changes the decision with its switches (next
section). The official start, when all three accounts begin at $25,000, is Monday 30 Nov 2026.

## System B's switches (phase X2)

B's decision layer sits around its judge. Each switch is in `config/b.yaml` under
`decision:` and can be turned off on its own; with all of them off B decides exactly like
A. For every idea B records what A's rules would have done and what B did instead, step
by step (the `b_decision_log` table), so each switch can later be measured against A on
the same ideas (phase X3).

| # | Switch | Default | What it does, simply |
|---|---|---|---|
| 1 | Honest confidence | ON | Leaves the pattern model out; turns the judge's confidence into "how often did ideas scored like this actually beat the market" (calibration), and skips trades the past data cannot vouch for (abstaining) |
| 2 | Risk sizing | ON | Stop 2 and target 3 "average daily ranges" away, instead of the judge's guesses; size so a stop-out loses at most 1% of the account; half size for new buys while SPY is below its 200-day average |
| 3 | Second look | ON | A cheaper model (Sonnet) reads each trade and may only keep it, shrink it or remove it, with a concrete reason. It can never add a trade or raise a size. If it fails, the trade stays as it was |
| 4 | Agent pruning | ON | Stops listening to agents whose record, after allowing for luck, is worse than a coin flip. They keep running in A; B starts listening again if they recover |
| 5 | Insider cluster buying | off | Shows the judge when 3+ insiders bought their own shares on the open market within 30 days. **Off until the backtest below says yes** |
| 6 | Against the sector | ON | Shows and measures each stock against its sector fund, not only against SPY |
| 7 | Cost band | off | Skips trades whose expected gain does not clearly beat trading costs |
| 8 | Options data | off | Implied volatility, call-put spread and skew (MarketData.app, paid, about $12/month) |
| 9 | Analyst data | off | Upgrades and downgrades, price targets, estimate revisions (FMP, paid, about $49/month) |
| 10 | Meta-labelling | off | A small model trained on past ideas that picks which ones to take; only used once it beats the simpler rules on recent ideas |

Honest notes:

* **Switch 1 waits for data.** Calibration needs 60 ideas whose outcome is known (each
  needs its holding period to pass), so for the first few weeks B uses the raw
  confidence and A's bar, and its log says "waiting". That is expected.
* **Switch 5 needs its backtest first.** It replays the rule over the insider filings
  already in the database, with no look-ahead, and gives a verdict by a rule fixed in
  advance (see `src/papertrader/b/backtest.py`). Run it, send me the output:

  ```bash
  docker compose exec app-b papertrader backtest insiders
  ```

* **Switches 8 and 9 cost money: don't subscribe yet.** Their code is ready and does
  nothing without a key. The docs sites of both services were blocked from where this was
  built, so the reply formats are checked carefully on arrival; after subscribing, one
  command verifies them:

  ```bash
  docker compose exec app-b papertrader paid-data-check
  ```

  My recommendation: decide after the first month of the X3 pilot, and only if B's core
  shows some signal. Adding data to a system that has none rarely creates it.
* **Not built:** the optional sector hedge of switch 6 (shorting the sector fund against
  each position), earnings-call transcripts, and short interest / borrow cost. The free
  sources for the last two could not be checked from here; they can be added once the
  paid services are in use and their formats can be verified.
* Every 08:00 B also records the insider clusters the rule would have used, even with
  switch 5 off, so the rule can be judged going forward too.

To see what is on, and the state of calibration and pruning:

```bash
docker compose exec app-b papertrader switches
```

### The Fable test (optional, off)

Two agents (news and earnings) also run on Claude Fable 5.2, on the same data as A's real
(Sonnet) run (the same moment, rebuilt from the same database), every trading day at 10:30. The Fable reports are never traded;
they are scored the same way as the real ones, so after a few months we can see whether
the stronger model's calls are more often right. It has its own budget of $10 a month,
separate from B's $30. To switch it on, set `decision: fable_shadow: enabled: true` in
`config/b.yaml`, then `docker compose restart app-b`.

## B's intraday scanner (phase X2b)

Besides its morning decision, B now watches the market during the day and tells you
before it buys.

**How it works.** Every 5 minutes from 09:45 to 15:30 New York time (15:45 to 21:30 in
Poland):

1. Simple rules check live prices for the whole S&P 500, at no cost: a stock breaking
   above its 20-day high on heavy volume, a big move up on heavy volume near the day's
   high, or a sharp dip inside an uptrend that is already bouncing.
2. The best few go to a quick, cheap screen (Claude Haiku) that passes on most of them.
3. The rest go to a decision (Claude Sonnet): buy or not, at what price, where the stop
   and the target are, how long the move should take, how confident it is, and why.
   The program then checks the plan (prices close to the market, a reward at least 1.5x
   the risk, a stop outside the stock's normal noise) and sizes it so a stop-out loses at
   most 1% of the account.
4. You get a Telegram message, for example:

   ```
   [B] PLANNED BUY · NVDA (intraday scanner)
   Buy 12 shares (about $1,574, 6.3% of the account), limit $131.20 (zone $130.60-$131.20)
   Target $135.80 (+3.5%) · stop $129.10 (-1.6%) · reward/risk 2.2
   Expected hold: about 2 hours · time stop Mon 13:40 New York
   Confidence 62%
   Why: ...
   Wrong if: ...
   Placing at 11:10 New York unless you send /skip NVDA
   ```

5. In **live** mode the order goes in after 5 minutes unless you send `/skip NVDA`, and
   the trade is closed when its expected time has clearly passed (a time stop). In
   **shadow** mode the message says "SHADOW (no order) · WOULD BUY", nothing is placed,
   and after the close the plan is scored on the full market's minute-by-minute prices,
   costs included, as if it had been placed.

**Limits.** At most 30 Claude calls, 8 decisions and 3 trades a day, $10 a month of
B's $30, and all of B's normal risk limits (positions, sectors, pause, kill switch). It
also respects the US day-trading rule: an account under $25,000 may make only 3 day
trades in 5 days, so the scanner stops placing when that limit is used. (Your real
$1,000 account would be under this rule too.)

**It starts in shadow mode.** After about two weeks, look at its record:

```bash
docker compose exec app-b papertrader scanner
```

It is judged on its own profit after costs. My rule: switch it to live only if the
shadow plans made money after costs, over at least 15 plans that would have filled;
otherwise leave it in shadow or turn it off. To switch: `mode: live` under
`decision: intraday:` in `config/b.yaml`, then `docker compose restart app-b`.

**IEX or the $99 SIP feed?** The free live prices (IEX) come from one exchange. The
scanner compares every price it used with the full market's price at the same minute
(available free 15 minutes later) and reports the typical gap. Stay on IEX unless that
gap is above 5 basis points (0.05%) or many plans fail to fill; only then is the $99 a
month SIP feed worth considering. Don't subscribe now.

Honest expectation: short-term set-ups like these are hard to profit from after costs,
and most of what the rules find will be passed on. That is why it starts in shadow mode
and has its own scorecard.

## B's post-mortems: "why did it move?" (phase X2c)

Every morning at 07:00 New York time (13:00 in Poland) B looks back at the previous
trading day and writes up to 10 short post-mortems, most important first:

* **closed trades**,
* **big moves** in stocks it holds (2+ typical daily ranges beyond the sector),
* **missed chances**: ideas it passed on or blocked that then went the predicted way,
* **dodged bullets**: ideas it passed on or blocked that then went the other way.

Each one says how the stock moved against SPY and its sector, the **likely causes with
how likely each is and the news or filings behind it** (the market, the sector or plain
noise are always among them), why B did what it did, whether that was **a good decision
with what was known at the time** (a good decision can lose money; hindsight does not
count), what would have changed it, and one lesson written as a condition ("when ...,
...").

On Telegram you get a short **"Why it moved"** digest after it runs, and `/why NVDA`
shows the latest post-mortem for a stock.

**Learning from it, without copying the past.** The point is not "it happened before, so
buy again". When B is about to trade, the most similar past cases are shown to a reviewer
**together with the differences from today** (a different kind of news, a different
market trend, different volatility), and the reviewer decides whether each case is really
comparable. Only comparable cases count, and only a little:

| Evidence | When | Largest nudge to the confidence |
|---|---|---|
| anecdote | one case | 0.03 |
| emerging | 3+ similar cases, 60%+ pointing the same way | 0.03 |
| supported | 8+ similar cases, 70%+ pointing the same way | 0.07 |

The level is counted by the program, never claimed by the model. A past case can make B
more careful (and lower a trade below its bar), but it **never turns a "no" into a
"yes"**, never makes a trade bigger and never loosens a stop or a risk limit. Old cases
(over a year) and cases that keep being judged not comparable are retired.

**Does it help?** Every use is recorded, and once a month the same ideas are scored with
and without the lessons. The result comes in the weekly message. After three months
without help, the recommendation is to switch the lessons off (the post-mortems can stay).

```bash
docker compose exec app-b papertrader postmortems
```

Cost: a cheap model per case and one digest a day, capped at $5 a month of B's $30. The
"Post-mortems" page comes with the iPhone web app (phase X5).

## How the systems are compared (phase X3)

The rules were written down **before** any experiment data existed, so they cannot be
bent to fit the results: [PREREGISTRATION.md](PREREGISTRATION.md). In short:

* **Early guide: signal quality.** Each day, did the ideas a system was most confident
  about do best over the next 5 days against their sector fund? (The "rank IC": 0 means
  no skill, above about 0.05 is useful.) Every idea counts, traded or not, scored at 1, 5
  and 10 days.
* **Final verdict: money.** After about 3 months (63 trading days): each system's profit
  **after its Claude and data costs**, compared with simply holding SPY, day by day, with
  statistics that do not let a lucky streak look like skill.
* **At your size.** Every report also shows each system as if it had run on your real
  $1,000: the trading gain, minus the same Claude and data costs. At $1,000, $25 a month
  of costs is 2.5% a month to make up before any profit.
* **The default answer is SPY.** If no system beats holding SPY after costs with real
  confidence, the recommendation is to hold SPY: the cheapest option.
* **C** is reported every month but judged only after two to three years.

**Every Saturday** (10:30 New York time, 16:30 in Poland) the hub sends a comparison
on Telegram: each system and SPY since the start, after costs, at your $1,000, the
signal quality and a plain-language verdict. In the first week of each month it adds
**B's switch review**: which of B's switches removed trades that would have made money
(or added trades that lost), with a recommendation to switch those off.

Any time:

```bash
docker compose exec hub papertrader experiment report
```

The web app's Compare tab shows the same numbers with charts ([WEBAPP.md](WEBAPP.md)).

### The official start: Monday 30 November 2026

All three systems start together on Monday 30 Nov (chosen on 22 Nov instead of 7 Dec: one
more week of results, and C's first weekly plan is made on Friday 27 Nov so C trades from
day one). The week before is the shake-down. The steps (you get a reminder for each):

1. **Friday 27 Nov, after the close (19:15 Polish time; a half day):** pause the systems
   on Telegram (`/pause A start`, `/pause B start`, `/pause C start`) and close any open
   positions (`docker compose exec app papertrader trade close TICKER`, and the same with
   `app-b` and `app-c`). C still makes its weekly plan that evening.
2. **Saturday 28 Nov, 10:00:** look at B's intraday scanner after one week in shadow mode
   (it stays in shadow mode unless it clearly earned its place).
3. **Sunday 29 Nov, 18:00:** check C's plan (`docker compose exec app-c papertrader
   portfolio`), reset each Alpaca paper account to $25,000 (new keys go in `.env`), then
   record the start and delete the equity history from before it:

   ```bash
   docker compose exec hub papertrader experiment start --date 2026-11-30 --clear-old-equity
   ```

   Then `/resume A`, `/resume B` and `/resume C` on Telegram.

## System C day to day (phase X4)

C holds up to 8 funds out of stocks, bonds, gold, commodities and the dollar, chosen by
fixed rules every week (see "Plan for system C" below for the rules and the backtest).

| When (New York) | What C does |
|---|---|
| 08:00 daily | Claude Haiku reads the night's news on C's funds; an alert only if something serious. Never trades |
| 09:35 Monday | Limit orders for the funds more than 20% off their target weight; anything unfilled is tried again on Tuesday |
| every 10 minutes | Order status; a resting stop 20% below every holding; 15% below the peak: all out, with an alert (back in after 4 weeks, by the rules); the -3% daily kill switch |
| 16:30 daily | Its funds' prices with dividends (its own data, from Alpaca) |
| 16:45 Friday | The signals, Claude Sonnet's review, and the **weekly plan on Telegram** |

The **weekly plan** message lists the target portfolio with the reason for every fund
("in: momentum +8.2%, 4 of 4 returns positive"), what is sold and why, the orders, the
review's verdicts, and **how to copy it on Avanza**: for each fund the matching European
product and the amount for your $1,000 (in dollars and roughly in SEK). Avanza's prices
are not available to the program, so buy the whole number of shares closest to each
amount. Check each product name on Avanza before buying: they are well-known funds, but
the program cannot see Avanza's list.

**Claude's only roles** are the weekly review, which may only reduce or skip a buy (never
add, never stop a sale), and the daily news watch. The review's effect is measured against
a "shadow C" that ignores it, shown in every weekly plan.

**The daily chart trades** are built but off: they are switched on only if the backtest
shows they add profit on the years it never saw, and then run in shadow mode first, with
"not for copying" on every message.

**Before C trades: the 20-year backtest.**

1. Make a free account at tiingo.com, copy the API token (Account > API) into `.env`:
   `TIINGO_API_KEY=...`, then `docker compose up -d`.
2. Run it (it downloads about 22 funds' history first, then takes a few minutes):

   ```bash
   docker compose exec app-c papertrader backtest c
   ```

3. Send me a photo of the output. I set `config/c.yaml` from it: the settings chosen on
   2004-2015, and only the upgrades that helped in both periods.

Any time: `docker compose exec app-c papertrader portfolio` shows the latest plan.

**The result (run on 21 Nov 2026, prices with dividends, after costs).**

| | 2004-2015 (settings chosen here) | 2016 to Nov 2026 (never seen) |
|---|---|---|
| C | +3.5% a year, Sharpe 0.36, worst fall -12% | +4.9% a year, Sharpe 0.39, worst fall -12% |
| SPY | +7.3% a year, Sharpe 0.40, worst fall -55% | +13.8% a year, Sharpe 0.69, worst fall -34% |
| 60/40 | +7.0% a year, Sharpe 0.57, worst fall -32% | +8.9% a year, Sharpe 0.65, worst fall -21% |

Chosen and now in `config/c.yaml`: momentum over 6 and 12 months, 8% volatility target,
the best 6 funds; idle cash in T-bills (the only upgrade that helped in both periods; the C
rows above are without it). Dual momentum, risk parity, managed futures and the daily chart
trades stay off. Honest reading: in the backtest C earned far less than SPY, and less than
60/40 even counting its smaller swings (lower Sharpe). Its one strength is the shallow
worst fall (-12% against -34% to -55%). C does not meet its target; it stays in the
experiment unchanged, because tuning it on the 2016-2026 years would make that test
meaningless. The decision rule (docs/PREREGISTRATION.md) still defaults to holding SPY.

## Telegram heads-ups (phase X6)

**What changes for the experiment.** Nothing waits for you: an order goes ahead unless
you tap **Skip**. A skip is a human decision, so it is recorded (the order warning
and, for sells, an exit note) and the weekly comparison counts it as part of that system.
If you want a clean test of B and C as built, don't skip; use Skip only for real
emergencies. A's warnings have no buttons, so A stays an untouched comparison.

**Timing.** Buys at the open and C's Monday orders are announced at 09:32 and go in at
09:35, as before. Sells the systems decide during the day (time limits, thesis breaks)
and B's intraday buys are announced and go 3 minutes later (or at once after Go now): a
one-minute check sends them. A skipped sell holds for that day and is asked again the
next day. Kill-switch and drawdown exits are never delayed.

**/ask** runs on Haiku with its own $2 a month and is left out of every system's costs.

## Plan for system C (phase X4)

C's rules are set with a 20-year backtest **before** it trades, then frozen.

**The backtest**

* Data: daily ETF prices with dividends included, from each ETF's launch (about 2004 on),
  from Tiingo (free key; you will be asked when X4 starts), cross-checked with Stooq and
  Alpaca. T-bill rates and gold from FRED. Covers 2008, 2020 and 2022.
* Honest test: the few settings (look-back periods, volatility target, number of ETFs
  held) are chosen on 2004-2015 only, then tested once on 2016-2026, which they never saw.
  Few settings, so the rules cannot simply memorise the past.
* Costs included (spreads, slippage). Compared with holding SPY and with a 60/40 portfolio:
  return, Sharpe ratio, worst drawdown, worst year. If C does not beat SPY after risk
  on the unseen years, the report says so.
* ETFs younger than the test (SGOV, DBMF, KMLM, later crypto) use a stand-in where one
  exists (BIL or the FRED T-bill rate for SGOV) or join from their launch date; the
  report says which.

**Upgrades on top of the original X4 rules**, each a switch. The backtest shows each
one's effect on its own, and only those that help on the unseen years are switched on:

1. Cash is held in a T-bill ETF (SGOV, BIL as backup) instead of earning nothing.
2. Dual momentum: an ETF is only eligible if its 12-month return also beats T-bills.
3. Weights account for correlation (risk parity), not only each ETF's own volatility.
   Same caps as before.
4. Managed-futures ETFs (DBMF, KMLM) in the universe, at most 15% together.
5. Fractional shares, needed for a real $1,000 account; whole shares for any ETF that
   cannot be bought in fractions.

**Copying C on Avanza.** EU rules stop private investors from buying US-listed ETFs, so
every C message also lists the matching European (UCITS) product for each ETF, e.g.
SPY -> iShares Core S&P 500 UCITS (CSPX), GLD -> a physical gold ETC, SGOV -> iShares
$ Treasury Bond 0-1yr UCITS (IB01) or a SEK money-market fund. Amounts are shown scaled
to your own capital (a setting), rounded to whole shares, with what is left over. The
list lives in `config/c.yaml` and is checked against what Avanza sells when X4 is built.
Results will differ a little (currency moves, Avanza's ~0.25% currency fee).

**C's daily chart trades (a separate part of C, with its own switch):**

* Short-term trades on the same ETFs, from chart rules (for example: a break above the
  recent high while the trend is up, or a dip inside an uptrend), at most 20% of C's money.
* Backtested on the same 20 years first, with costs, and only switched on if it adds
  profit after costs on the years it never saw. Then 2-4 weeks in shadow mode (messages
  only, no orders).
* Judged on its own profit after costs, apart from C's weekly portfolio, and switched off
  if it does not add any. The same risk limits apply (daily kill switch, the drawdown
  rules).
* Honest expectation: short-term chart trading has much weaker evidence than weekly
  trend following, and copying daily trades by hand on Avanza is hard and costly, so
  these messages are marked "not for copying" unless you change that.

**Telegram heads-up for C:**

* Weekly portfolio: the plan for Monday is sent on Friday after the close (what to buy
  and sell, why, the Avanza products and amounts), so there is time to copy it.
* Daily trades: a PLANNED BUY or PLANNED SELL a few minutes before the order (default 3,
  phase X6), with the price, stop, target, expected holding time, and which chart rule
  fired, in plain words. A warning when a position gets close to its stop or target.

**For B (phase X2):** insider cluster buying and analyst revisions are backtested on
history before they are switched on (SEC Form 4 back to 2003, free; FMP's estimate
history with the paid plan). The Claude agents cannot be backtested honestly, because
the models already know how past events turned out; they are judged only going forward.

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
`web` and `backup` should say `Up`. On Telegram, send `/status`: you get an answer
from `[A]`, and from `[B]` too within 10 minutes (the hub looks for newly set-up systems
every 10 minutes).

## Day to day

* Every Telegram message starts with `[A]`, `[B]` or `[C]`.
* Commands take a system: `/pnl B`, `/positions A`, `/pause B earnings week`, `/resume B`.
  `/skip NVDA` cancels B's planned intraday trade in NVDA before it is placed.
  `/why NVDA` shows B's latest "why did it move?" post-mortem for NVDA.
  Without one, `/status`, `/positions`, `/pnl` and `/brief` answer for every system.
  `/pause` and `/resume` always need the system.
* Logs: `docker compose logs -f app` (A), `docker compose logs -f app-b` (B),
  `docker compose logs -f hub` (Telegram).
* One system's command line: `docker compose exec app-b papertrader check`
  (B), `docker compose exec app papertrader check` (A).
* The web app (X5) shows all three systems ([WEBAPP.md](WEBAPP.md)).
* Backups include all three systems (they are all in the one database).

## The daily pipeline (New York time)

| Time | What |
|---|---|
| 06:30 | A loads the shared market data (news, filings, prices) for everyone |
| 07:00 | B's post-mortems of the day before, then the "Why it moved" digest |
| 07:30 | A's research agents |
| 08:00 | B's extra signals (insider clusters; options and analyst data if switched on) |
| 08:20 | A's pattern model and judge |
| 08:25 | B copies A's agent reports, runs its own judge on them, then its switches |
| 08:45 | Morning briefs, tagged [A] and [B] |
| 08:00 | C's news watch |
| 09:35 | Orders (each system on its own account; C on Mondays) |
| 09:45-15:30 | B's intraday scanner, every 5 minutes (shadow mode at first) |
| 10:30 | B's Fable test, if switched on (never traded) |
| every 10 min | Each system watches its own positions |
| 16:15 | End-of-day reconcile |
| 16:30 / 16:45 | C's prices; on Fridays C's weekly plan |
| 17:00 / 17:10 | A, then B, score their past calls |
| Saturday | Weekly reports; 10:30 the hub's A vs B vs C comparison |

B only reuses A's reports while the agent settings and prompts are exactly the frozen
ones (it checks a fingerprint every morning). If they ever differ, B refuses and alerts
you instead of judging reports it would not have got itself.

## The freeze rules

So the comparison stays fair, A must not change during the experiment:

* Never rebuild `papertrader:a-frozen`. `docker compose up -d --build` rebuilds only B,
  C, the hub and the web app; A's image is left alone.
* Never edit `config/a.yaml`. (B's settings are `config/b.yaml`, C's `config/c.yaml`.
  `config.yaml` is now only used by the hub and the web app.)
* The shared database layout does not change: new tables for B or C go in their own
  sections. Automated tests enforce all three rules.
* If a bug has to be fixed for everyone (for example a data source changes its format),
  it is written down below and applied to all systems, including a rebuilt A image.

## Changes during the experiment

| Date | Change | Applied to |
|---|---|---|
| 2026-11-20 | A frozen at commit `e54f521` | A |
| 2026-11-20 | B's decision layer (phase X2) | B only |
| 2026-11-20 | B's intraday scanner, in shadow mode (phase X2b) | B only |
| 2026-11-20 | B's post-mortems and capped lessons (phase X2c) | B only |
| 2026-11-20 | The measurement rules pre-registered (docs/PREREGISTRATION.md, phase X3) | all |
| 2026-11-21 | System C built (phase X4); its settings follow the backtest before the start | C only |
| 2026-11-21 | C's settings set from its 20-year backtest (6 and 12-month momentum, 8% volatility, best 6 funds, cash in T-bills) | C only |
| 2026-11-21 | The iPhone web app replaces the dashboard; the hub stores live account numbers for it (phase X5) | none (display only) |
| 2026-11-22 | Telegram upgrade (phase X6): B and C announce each order 3 minutes ahead with Skip / Go now; A's heads-ups are information only, written by the hub | B and C: an order you skip is not sent. A unchanged |
| 2026-11-22 | Official start set to Monday 30 Nov 2026 (instead of 7 Dec) | all |

## How the data is kept apart (for the curious)

All systems use the one PostgreSQL database. The market data (prices, news, filings,
...) is loaded once, by A, and read by everyone. A's own tables stay where they always
were; B's and C's decisions, orders, trades, outcomes, messages and spend are in their
own sections (`sys_b`, `sys_c`), which B and C create for themselves on first start.
A system can read the shared market data but never another system's decisions.
Details: [ARCHITECTURE.md](../ARCHITECTURE.md), "The three-way experiment".
