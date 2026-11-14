"""The backtest report, as Markdown and as a standalone HTML page.

The report is written to be read by a non-expert. It leads with the warnings, because
a backtest is the easiest thing in finance to fool yourself with.
"""

from __future__ import annotations

import html
from typing import Any

DISCLAIMER = [
    "**This is an experiment, not advice, and nothing here is a promise of profit.**",
    "**Backtests flatter.** Every number below comes from replaying the past. Real trading "
    "adds delays, partial fills, wider spreads on news days and plain bad luck.",
    "**Leakage through Claude.** Where text features extracted by Claude are used, Claude may "
    "have read about these events and how the stocks then moved. Headlines are shown to it "
    "with company names, tickers and dates masked, which reduces this but cannot remove it. "
    "Treat any result that depends on those features as optimistic.",
    "**Survivorship bias.** Only today's index members are in the data. Companies that were "
    "dropped from the index, often after falling, are missing, which makes the past look better.",
    "**Adjusted prices** are today's view of history, not what was on screen that day.",
    "**Forward paper trading is the real test.** Only results on days after the model was "
    "trained, traded on paper as they happen, say anything about whether this works.",
]


def pct(value: Any, digits: int = 1) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.{digits}f}%"


def num(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def sanity_warnings(report: dict[str, Any]) -> list[str]:
    """Flags results that are too thin to mean anything, or too good to be true."""
    notes: list[str] = []
    oos = report.get("oos", {})
    rows, days = oos.get("rows", 0), report.get("backtest", {}).get("candidate_days", 0)
    strategy = report.get("backtest", {}).get("strategy", {})
    random = report.get("backtest", {}).get("random", {})

    if rows < 2000 or days < 250:
        notes.append(
            f"**Too little data to conclude anything.** {rows:,} out-of-sample rows over {days} "
            "days is a small sample; differences of this size appear by chance all the time."
        )
    too_good = []
    if (oos.get("ic_daily") or 0) > 0.08:
        too_good.append(f"daily IC {num(oos.get('ic_daily'))}")
    if (oos.get("hit_rate_top_decile") or 0) > 0.62:
        too_good.append(f"top-decile hit rate {pct(oos.get('hit_rate_top_decile'))}")
    if (strategy.get("sharpe") or 0) > 2.0:
        too_good.append(f"Sharpe {num(strategy.get('sharpe'), 2)}")
    if too_good:
        notes.append(
            "**These results look too good.** " + ", ".join(too_good) + ". Numbers like these on "
            "daily stock data usually mean information from the future leaked into the features "
            "or labels. Assume a bug or leakage until proven otherwise."
        )
    percentile = random.get("strategy_percentile")
    if percentile is not None and 0.05 < percentile < 0.95:
        notes.append(
            f"**Not distinguishable from random picks.** The strategy beat {pct(percentile, 0)} of "
            f"{random.get('runs')} random selections from the same candidates. Anything between "
            "5% and 95% is what chance alone produces."
        )
    if (oos.get("ic_daily") or 0) <= 0 and rows:
        notes.append("**No ranking skill out of sample.** The daily IC is zero or negative.")
    brier, naive = oos.get("brier"), oos.get("brier_naive")
    if brier is not None and naive is not None and brier >= naive:
        notes.append(
            "**Probabilities add nothing.** The Brier score is no better than always predicting "
            "the base rate."
        )
    return notes


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def to_markdown(report: dict[str, Any]) -> str:
    data, oos, bt = report["data"], report.get("oos", {}), report.get("backtest", {})
    horizon = report["horizon"]
    out = [
        f"# Pattern model backtest - {report['version']}",
        "",
        f"Generated {report['generated_at']}.",
        "",
    ]

    out += ["## Read this first", ""] + [f"- {line}" for line in DISCLAIMER] + [""]
    warnings = sanity_warnings(report)
    out += ["## What these results do and do not show", ""]
    out += [f"- {line}" for line in warnings] or [
        "- No automatic warning was triggered. That is not proof of an edge."
    ]
    out += [""]

    out += ["## Data", ""]
    out += _table(
        ["Item", "Value"],
        [
            ["Rows (one stock on one day with an event)", f"{data['rows']:,}"],
            ["Stocks", str(data["tickers"])],
            ["Period", f"{data['first_day']} to {data['last_day']}"],
            [
                "Target",
                f"beats SPY over {horizon} trading days (open of day 1 to close of day {horizon})",
            ],
            ["Share of rows that beat SPY", pct(data.get("base_rate"))],
            [
                "Rows with Claude text features",
                f"{data.get('llm_rows', 0):,} ({pct(data.get('llm_share'))})",
            ],
        ],
    )
    out += [
        "",
        "Events in the data: "
        + ", ".join(f"{k} {v:,}" for k, v in data.get("events", {}).items())
        + ".",
        "",
    ]

    out += ["## Walk-forward validation", "",
            f"The model is always tested on days after its training data, with "
            f"{report['embargo_days']} trading days removed in between so labels cannot overlap. "
            "The data is never shuffled.", ""]  # fmt: skip
    out += _table(
        ["Fold", "Trained through", "Tested", "Test rows", "Hit rate", "Daily IC", "Brier"],
        [[str(f["fold"]), f["train_through"], f"{f['test_from']} to {f['test_to']}", f"{f['test_rows']:,}",
          pct(f.get("hit_rate")), num(f.get("ic_daily")), num(f.get("brier"))] for f in report.get("folds", [])],
    )  # fmt: skip
    out += [f"- Skipped: {note}" for note in report.get("skipped", [])] + [""]

    out += ["## Out-of-sample accuracy (all test folds together)", ""]
    out += _table(
        ["Measure", "Value", "What it means"],
        [
            ["Hit rate", pct(oos.get("hit_rate")), f"direction right; a stock beats SPY {pct(oos.get('base_rate'))} of the time anyway"],
            ["Hit rate, most confident 10%", pct(oos.get("hit_rate_top_decile")), "direction right on the model's strongest calls"],
            ["Information coefficient (daily)", num(oos.get("ic_daily")), "rank correlation of probability and outcome within a day; 0 = no skill, 0.02-0.05 would be good"],
            ["Information coefficient (pooled)", num(oos.get("ic")), "the same over all rows at once"],
            ["Brier score", num(oos.get("brier")), f"error of the probabilities, lower is better; always guessing the base rate gives {num(oos.get('brier_naive'))}"],
            ["AUC", num(oos.get("auc")), "0.5 = coin flip"],
            ["Mean excess return, most confident 10%", pct(oos.get("mean_excess_top_decile"), 2), f"vs SPY over {horizon} days, before costs"],
        ],
    )  # fmt: skip

    out += ["", "## Calibration", "",
            f"Method: {report.get('calibration_method', 'n/a')}, fitted on scores for days the "
            "model had not seen, and pulled toward 50% when there are few such examples. If the "
            "probabilities are honest, 'predicted' and 'actual' are close in every row.", ""]  # fmt: skip
    out += _table(["Predicted", "Actual", "Rows"],
                  [[pct(r["predicted"]), pct(r["actual"]), f"{r['rows']:,}"] for r in report.get("calibration", [])])  # fmt: skip

    if bt:
        s, b, r = bt["strategy"], bt["benchmark"], bt["random"]
        out += ["", "## Strategy backtest with baselines", "",
                f"Rule: each day buy up to {bt['top_n']} candidates with probability of at least "
                f"{bt['threshold']:.2f}, {pct(report['position_pct'], 0)} of the account each, at most "
                f"{report['max_positions']} positions, hold {horizon} days. Costs: "
                f"{bt['cost_bps_per_side']:.0f} basis points per side (commission plus slippage). "
                f"Period {bt['from']} to {bt['to']}, {bt['picks']} picks on {bt['candidate_days']} candidate days.", ""]  # fmt: skip
        out += _table(
            ["", "Strategy", "SPY buy and hold", f"Random picks (mean of {r['runs']})"],
            [
                ["Total return", pct(s.get("total_return")), pct(b.get("total_return")),
                 f"{pct(r['total_return_mean'])} (5%-95%: {pct(r['total_return_p05'])} to {pct(r['total_return_p95'])})"],
                ["Return per year", pct(s.get("annual_return")), pct(b.get("annual_return")), ""],
                ["Sharpe ratio", num(s.get("sharpe"), 2), num(b.get("sharpe"), 2), num(r["sharpe_mean"], 2)],
                ["Max drawdown", pct(s.get("max_drawdown")), pct(b.get("max_drawdown")), ""],
                ["Trades", str(s.get("trades", 0)), "1", ""],
                ["Turnover per year", f"{s.get('turnover', 0):.1f}x the account", "0", ""],
            ],
        )  # fmt: skip
        out += ["", f"The strategy beat {pct(r['strategy_percentile'], 0)} of the random selections. "
                    "Random picks hold the same number of stocks on the same days from the same "
                    "candidates, so this is the fairest test of whether the model's choices matter.", ""]  # fmt: skip

    out += ["## What the model relies on", ""]
    out += _table(
        ["Feature", "Share of importance"],
        [[name, pct(share)] for name, share in report.get("importance", [])],
    )
    out += ["", "## Promotion", "", f"- {report['promotion']}", ""]
    return "\n".join(out)


def _inline(text: str) -> str:
    escaped = html.escape(text)
    while "**" in escaped:
        escaped = escaped.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    return escaped.replace("`", "")


def to_html(markdown: str, title: str) -> str:
    """A minimal Markdown-to-HTML conversion for exactly what `to_markdown` produces."""
    body: list[str] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            body.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([cell.strip() for cell in lines[i].strip("|").split("|")])
                i += 1
            head, data = rows[0], rows[2:]
            table = (
                "<table><thead><tr>"
                + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead><tbody>"
            )
            table += "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in data
            )
            body.append(table + "</tbody></table>")
            continue
        elif line.strip():
            body.append(f"<p>{_inline(line)}</p>")
        i += 1
    style = (
        "body{font:16px/1.5 system-ui,sans-serif;max-width:920px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
        "table{border-collapse:collapse;margin:1rem 0;width:100%}th,td{border:1px solid #ccc;padding:.4rem .6rem;"
        "text-align:left;vertical-align:top}th{background:#f2f2f2}h2{margin-top:2rem}"
        "ul:first-of-type{background:#fff6e0;border:1px solid #e0b040;padding:1rem 1rem 1rem 2rem}"
    )
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{style}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )
