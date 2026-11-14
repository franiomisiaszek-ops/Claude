"""Plain-text rendering of a cycle, used by `papertrader dry-run`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from papertrader.ingest.timeutil import NEW_YORK
from papertrader.research.cycle import CycleResult

LEAKAGE_NOTICE = (
    "NOTE: this is a run on a past date. Claude was trained on data that may include what\n"
    "happened afterwards, so results on past dates are optimistic and prove nothing.\n"
    "Only forward paper trading is a real test."
)


def render(result: CycleResult, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    when = result.as_of.astimezone(NEW_YORK).strftime("%A %Y-%m-%d %H:%M New York time")
    out = [f"=== Decision as of {when} ({result.kind}) ===", ""]

    out.append(f"Budget: {result.budget.mode} - {result.budget.reason}. "
               f"Month so far ${result.budget.month_spent:.2f} of ${result.budget.month_budget:.2f}.")  # fmt: skip
    if result.prices_through is not None:
        out.append(f"Data: {result.covered} stocks with prices through {result.prices_through}.")
    out += [f"* {note}" for note in result.notes]
    if now - result.as_of > timedelta(days=1):
        out += ["", LEAKAGE_NOTICE]

    out += ["", "--- Agents ---"]
    for run in result.runs:
        line = f"{run.label:<40} {run.status:<10}"
        if run.status == "ok":
            line += f" {run.reports} report(s)"
            if run.dropped_claims or run.dropped_reports:
                line += (f", removed {run.dropped_claims} uncited claim(s) and "
                         f"{run.dropped_reports} report(s)")  # fmt: skip
        if run.cost_usd:
            line += f"  ${run.cost_usd:.3f}"
        if run.detail and run.status != "ok":
            line += f"  ({run.detail[:110]})"
        out.append(line)

    if result.status == "estimated":
        out += ["", f"Estimated cost of a full run: about ${result.cost_usd:.2f} "
                    "(no API calls were made)."]  # fmt: skip
        return "\n".join(out)

    if result.reports:
        out += ["", "--- Reports ---"]
        for report in sorted(result.reports, key=lambda r: (r.ticker, -r.confidence)):
            who = report.agent_name + (f" [{report.sector}]" if report.sector else "")
            out.append(
                f"#{report.report_id} {report.ticker:<6} {report.direction.upper():<7} "
                f"conf {report.confidence:.2f}  {report.horizon_days}d  {report.event_type}  by {who}"
            )
            out.append(f"    {report.thesis}")
            out += [f"    - {e.claim}  <{e.source_id}>" for e in report.evidence]

    decision = result.decision
    out += ["", "--- Judge ---"]
    if decision is None:
        out.append("No decision.")
    else:
        out += [decision.market_summary, ""]
        if not decision.trades:
            out.append("No trades proposed.")
        for trade in decision.trades:
            model = "n/a" if trade.model_probability is None else f"{trade.model_probability:.2f}"
            verdict = {"proposed": "WOULD TRADE", "below_threshold": "not traded",
                       "rejected_by_risk": "REJECTED"}[trade.status]  # fmt: skip
            out.append(
                f"{trade.rank}. {trade.action.upper()} {trade.ticker}  final confidence "
                f"{trade.final_confidence:.0%}  [{verdict}]"
            )
            out.append(
                f"   judge {trade.judge_confidence:.2f} | model {model} | agent agreement "
                f"{trade.agent_agreement:.2f}  (threshold {decision.threshold:.2f})"
            )
            out.append(
                f"   entry {trade.entry_low:.2f}-{trade.entry_high:.2f}, target "
                f"{trade.target_price:.2f}, stop {trade.stop_price:.2f}, size "
                f"{trade.size_pct:.0%}, {trade.horizon_days} days"
            )
            out.append(f"   why: {trade.rationale}")
            if trade.dissent:
                out.append(f"   against: {trade.dissent}")
            if trade.status_reason:
                out.append(f"   note: {trade.status_reason}")
            out.append(f"   supporting reports: {trade.supporting}  opposing: {trade.opposing}")
        if decision.rejected:
            out += ["", "Rejected ideas:"]
            out += [f"   {idea.ticker}: {idea.reason}" for idea in decision.rejected]
        if decision.exits:
            out += ["", "Exits:"] + [f"   {e.ticker}: {e.reason}" for e in decision.exits]

    calls = [run for run in result.runs if run.status == "ok" and run.label != "judge"]
    cached = [run for run in calls if run.cache_read_tokens]
    if calls:
        out += ["", f"Prompt cache: {len(cached)} of {len(calls)} agent calls read the shared "
                    f"rules from the cache ({sum(r.cache_read_tokens for r in cached):,} tokens "
                    "at a tenth of the price or less)."]  # fmt: skip
    out += ["", f"Cost of this run: ${result.cost_usd:.3f}"]
    return "\n".join(out)
