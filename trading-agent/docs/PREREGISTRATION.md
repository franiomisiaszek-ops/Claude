# Pre-registration: the three-way experiment

These rules were written and committed **before the experiment's data existed** (see the
date of this file's first commit in git). They are not changed after the start. If a
change is ever unavoidable, it is written in the table at the bottom with its date and
reason, and the affected test starts its clock again.

Everything here is paper trading. The program has no way to trade real money; using real
money is always a separate decision by the owner.

## What is compared

| System | What it is |
|---|---|
| A | The original system, frozen at commit `e54f521` |
| B | A's research plus B's decision layer (switches in `config/b.yaml`), the intraday scanner and the post-mortems |
| C | The weekly ETF trend portfolio (phase X4) |
| SPY | Simply holding the S&P 500 fund: the benchmark every system must beat |

All three start on the same day with $25,000 each, on separate Alpaca paper accounts.

## Start and length

* **Start**: the date recorded by `papertrader experiment start` (kept in the database and
  printed on every report).
* **Pilot**: the first 21 trading days (about a month). It checks that everything works;
  no verdict is given. Pilot days count toward the test, unless a fix during the pilot
  changes how a system decides; then that system's test window restarts the day after
  the fix (written below).
* **A vs B test**: 63 trading days (about three months) from the start.
* **C**: reported every month, judged only after two to three years: a three-month test
  cannot judge trend following, which earns its keep in rare, long moves.

## Hypotheses

Tested together, with Holm's correction for testing three things at once.

* **H1, signal quality**: B's ideas are ranked better than A's.
  Measured as the daily rank IC of B minus that of A.
* **H2, money**: B makes more than A after costs.
  Measured as B's daily net return minus A's.
* **H3, beating the market**: the better of A and B (by net return over the test) beats
  holding SPY after costs. Measured as its daily net return minus SPY's daily return.

## Metrics

**Primary signal metric: daily rank IC.** Each trading day, for each system, the
Spearman rank correlation between the confidence the system gave its ideas that morning
(all ideas its judge scored, traded or not; a short counts as the negative) and the
ideas' **5-trading-day returns minus their sector fund's** (in at the open of the
decision day, out at the close of day 5). Days with fewer than 3 scored ideas are left
out for that system.

Secondary signal metrics: the Brier score of the same confidences read as the chance of
beating the sector over 5 days; the hit rate; the same at 1 and 10 days.

**Money metric (the final verdict): net return against SPY.** Each day: the change in
the account's end-of-day equity, minus that day's Claude spend and 1/21 of the month's
data costs, divided by the previous day's equity; minus SPY's total return that day.

Every idea is scored, whether it was traded or not ("shadow scoring"), using only prices
from after its decision time.

## Statistics

* Everything is paired by date: each test uses the daily differences (system minus
  system, or system minus SPY) on the days both have a value.
* The estimate is the mean daily difference. Its 95% interval comes from a moving-block
  bootstrap (blocks of 5 days, 5,000 resamples, fixed seed), because 5-day returns
  overlap and neighbouring days are related. A t-statistic with a Newey-West standard
  error (5 lags) is reported next to it, and its two-sided p-value is the one that
  is Holm-corrected.
* Nothing is dropped: no "bad days" removed, no ideas left out after the fact.

## Decision rule

After the 63-day test:

1. **A system beats SPY** when H3 holds for it: Holm-corrected p below 0.05 and the 95%
   interval above zero. It is then the candidate, still on paper.
2. **If no system beats SPY after costs, the recommendation is to hold SPY**: the cheapest
   option. A system that is ahead but not significantly may continue on paper once, for
   another 63 days.
3. **B vs A**: if H1 or H2 holds for B, B's decision layer stays. If neither does, B is cut
   back to its cheapest form: the switches that did not add net profit in the monthly
   switch review are turned off.
4. **Costs at real size**: a system whose net profit is below zero when its returns are
   scaled to $1,000 of real capital, with its real Claude and data costs, is not
   recommended for real money, even if it beats SPY at $25,000.
5. **C** is judged separately after two to three years: a Sharpe ratio at least SPY's,
   with a largest drawdown at most half of SPY's. Before that it is reported monthly,
   with no verdict.

## Monthly switch review (system B)

Every month, for each of B's switches, the ideas it changed (removed, added, resized) are
scored at 5 days against A's decision on the same idea. A switch whose removed ideas
would have made money, or whose added ideas lost money, over at least 20 changed ideas,
is recommended off. Agents are handled by switch 4 (pruning).

## Never done

* Changing these rules after the start without writing it below.
* Look-ahead: an idea is scored only from its decision time on.
* Dropping days, trades or ideas that look bad.
* Trading real money.

## Changes after the start

| Date | Change | Why | Effect on the test |
|---|---|---|---|
| 2026-11-22 | Telegram heads-ups with a Skip button for B's and C's orders (phase X6); A has none | So you can stop an order in an emergency | Decided before the start. A skipped order counts as part of that system's result; every skip is recorded (`order_warnings`). Skips are meant for emergencies only |
