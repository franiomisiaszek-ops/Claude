"""Altair charts for the dashboard.

Colors follow one fixed categorical order (validated for color-blind readers in light
and dark mode). Each series keeps the color of its slot whatever else is shown:
papertrader is always the first color, SPY the second, each agent its own. Thin 2px
lines, recessive grid, a legend for two or more series, a tooltip on hover, and a
table next to every chart.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, cast

import altair as alt
import pandas as pd

LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]
AGENTS = ("macro", "earnings", "sec_filings", "sector_analyst", "news_event", "technicals",
          "insider_institutional", "contrarian")  # fmt: skip
SPEND_GROUPS = ("research agents", "judge", "thesis checks", "headline labels")
SERIES = ("papertrader", "SPY")
SYSTEMS = ("A", "B", "C", "SPY")

Theme = Literal["light", "dark"]
AnyChart = alt.Chart | alt.LayerChart


def palette(theme: Theme) -> list[str]:
    return DARK if theme == "dark" else LIGHT


def present_in_order(values: Iterable[str], order: Sequence[str]) -> list[str]:
    seen = set(values)
    return [name for name in order if name in seen] + sorted(seen - set(order))


def color_scale(present: Sequence[str], order: Sequence[str], theme: Theme) -> alt.Scale:
    """Only the series present appear in the legend, each in the color of its fixed slot."""
    colors = palette(theme)
    slots = list(order) + [name for name in present if name not in order]
    return alt.Scale(
        domain=list(present), range=[colors[slots.index(n) % len(colors)] for n in present]
    )


def _time_axis() -> alt.Axis:
    return alt.Axis(format="%d %b", labelAngle=0, labelOverlap=True, grid=False, title=None)


def _legend() -> alt.Legend:
    return alt.Legend(title=None, orient="top", columns=4)


def returns_chart(curve: pd.DataFrame, theme: Theme = "light") -> AnyChart:
    """papertrader vs SPY, % since the first snapshot."""
    long = curve.melt(id_vars="day", value_vars=["papertrader_pct", "spy_pct"], var_name="series",
                      value_name="return %").dropna()  # fmt: skip
    long["series"] = long["series"].map({"papertrader_pct": "papertrader", "spy_pct": "SPY"})
    present = present_in_order(long["series"], SERIES)
    base = alt.Chart(long).encode(
        x=alt.X("day:T", timeUnit="yearmonthdate", axis=_time_axis()),
        y=alt.Y("return %:Q", axis=alt.Axis(title="% since start", gridOpacity=0.4)),
        color=alt.Color("series:N", scale=color_scale(present, SERIES, theme), legend=_legend()),
        tooltip=[alt.Tooltip("day:T", format="%a %d %b %Y"), "series:N", alt.Tooltip("return %:Q", format="+.2f")],
    )  # fmt: skip
    chart = (base.mark_line(strokeWidth=2) + base.mark_point(size=64, filled=True)).properties(
        height=320
    )
    return cast(AnyChart, chart)


def drawdown_chart(curve: pd.DataFrame, theme: Theme = "light") -> AnyChart:
    color = palette(theme)[0]
    base = alt.Chart(curve).encode(
        x=alt.X("day:T", timeUnit="yearmonthdate", axis=_time_axis()),
        y=alt.Y("drawdown_pct:Q", axis=alt.Axis(title="% below the best level so far", gridOpacity=0.4)),
        tooltip=[alt.Tooltip("day:T", format="%a %d %b %Y"),
                 alt.Tooltip("drawdown_pct:Q", title="drawdown %", format=".2f")],
    )  # fmt: skip
    chart = (base.mark_area(color=color, opacity=0.25) + base.mark_line(color=color, strokeWidth=2)
             + base.mark_point(color=color, size=64, filled=True)).properties(height=200)  # fmt: skip
    return cast(AnyChart, chart)


def spend_chart(daily: pd.DataFrame, theme: Theme = "light") -> AnyChart:
    """Stacked dollars per day by spending group; one bar per day (an ordinal axis, so a
    single day is one bar, not the whole width)."""
    long = daily.reset_index().melt(id_vars="day", var_name="group", value_name="dollars")
    long = long[long["dollars"] > 0].copy()
    long["label"] = long["day"].dt.strftime("%d %b")
    present = present_in_order(long["group"], SPEND_GROUPS)
    days = list(dict.fromkeys(long.sort_values("day")["label"]))
    chart = alt.Chart(long).mark_bar(size=24, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("label:O", sort=days, axis=alt.Axis(labelAngle=0, labelOverlap=True, title=None)),
        y=alt.Y("sum(dollars):Q", axis=alt.Axis(title="$ per day", format="$.2f", gridOpacity=0.4)),
        color=alt.Color("group:N", scale=color_scale(present, SPEND_GROUPS, theme), legend=_legend()),
        order=alt.Order("group:N"),
        tooltip=[alt.Tooltip("label:O", title="day"), "group:N",
                 alt.Tooltip("sum(dollars):Q", title="dollars", format="$.3f")],
    ).properties(height=260)  # fmt: skip
    return cast(AnyChart, chart)


def weights_chart(history: pd.DataFrame, theme: Theme = "light") -> AnyChart:
    present = present_in_order(history["agent"], AGENTS)
    base = alt.Chart(history).encode(
        x=alt.X("week:T", timeUnit="yearmonthdate", axis=_time_axis()),
        y=alt.Y("weight:Q", scale=alt.Scale(zero=False),
                axis=alt.Axis(title="weight (1.0 = no track record)", gridOpacity=0.4)),
        color=alt.Color("agent:N", scale=color_scale(present, AGENTS, theme), legend=_legend()),
        tooltip=[alt.Tooltip("week:T", format="%d %b %Y"), "agent:N", alt.Tooltip("weight:Q", format=".2f")],
    )  # fmt: skip
    chart = (base.mark_line(strokeWidth=2) + base.mark_point(size=64, filled=True)).properties(
        height=260
    )
    return cast(AnyChart, chart)


def experiment_chart(rows: pd.DataFrame, theme: Theme = "light") -> AnyChart:
    """A, B and C after costs, and SPY: % since the experiment's start."""
    present = present_in_order(rows["series"], SYSTEMS)
    base = alt.Chart(rows).encode(
        x=alt.X("day:T", timeUnit="yearmonthdate", axis=_time_axis()),
        y=alt.Y("return %:Q", axis=alt.Axis(title="% since the start, after costs", gridOpacity=0.4)),
        color=alt.Color("series:N", scale=color_scale(present, SYSTEMS, theme), legend=_legend()),
        tooltip=[alt.Tooltip("day:T", format="%a %d %b %Y"), "series:N", alt.Tooltip("return %:Q", format="+.2f")],
    )  # fmt: skip
    return cast(AnyChart, (base.mark_line(strokeWidth=2)).properties(height=320))
