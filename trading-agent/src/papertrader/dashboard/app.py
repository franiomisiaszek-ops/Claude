"""Streamlit entry point:  streamlit run src/papertrader/dashboard/app.py

Read-only: the database connection is opened in read-only mode, so this page
cannot change anything even by accident.
"""

from __future__ import annotations

import streamlit as st

from papertrader import __version__
from papertrader.config import ConfigError, get_config, get_settings
from papertrader.dashboard.data import load_status
from papertrader.db.session import make_engine
from papertrader.safety import ALPACA_PAPER_BASE_URL

st.set_page_config(page_title="papertrader", layout="wide")


@st.cache_resource
def _engine():  # type: ignore[no-untyped-def]
    return make_engine(get_settings().database_url, read_only=True)


st.title("papertrader")
st.caption(
    f"Version {__version__} - PAPER TRADING ONLY ({ALPACA_PAPER_BASE_URL}). "
    "An experiment, not advice."
)

try:
    config = get_config()
except ConfigError as exc:
    st.error(f"config.yaml problem: {exc}")
    st.stop()

try:
    status = load_status(_engine())
except Exception as exc:
    st.error(f"Cannot reach the database: {exc}")
    st.stop()

if not status.schema_ready:
    st.warning(
        "The database has no tables yet. Start the app service (it creates them) and reload."
    )
    st.stop()

left, middle, right = st.columns(3)
left.metric("App service", "running" if status.app_alive else "not running")
middle.metric(
    "Last heartbeat",
    "never" if status.heartbeat_age_seconds is None else f"{status.heartbeat_age_seconds:.0f}s ago",
)
right.metric("Monthly API budget", f"${config.budget.monthly_usd:,.0f}")

st.subheader("Settings in use")
st.table(
    {
        "Setting": ["Universe", "Research agents", "Judge", "Trade threshold", "Max per position",
                    "Max positions", "Max per sector", "Shorts", "Daily loss kill switch"],
        "Value": [
            config.universe.index,
            f"{config.agents.defaults.model} (effort {config.agents.defaults.effort})",
            f"{config.judge.model} (effort {config.judge.effort})",
            f"{config.judge.confidence_threshold:.0%} confidence",
            f"{config.risk.max_position_pct:.0%} of equity",
            str(config.risk.max_open_positions),
            f"{config.risk.max_sector_pct:.0%} of equity",
            "allowed" if config.risk.allow_shorts else "disabled",
            f"-{config.risk.daily_loss_kill_switch_pct:.0%}",
        ],
    }
)  # fmt: skip

st.subheader("Database tables")
st.dataframe(
    {"Table": list(status.table_rows), "Rows": list(status.table_rows.values())},
    hide_index=True,
    use_container_width=True,
)
st.info(
    "Phase 1 status page. Equity curve, trades, agent reports, leaderboard, model metrics and "
    "API spend pages are added in Phase 8."
)
