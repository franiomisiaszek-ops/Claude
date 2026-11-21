"""Configuration.

Two sources, kept strictly apart:

* `config.yaml`  - behaviour (schedule, risk limits, models, budget). Safe to commit.
* `.env`         - secrets and machine-specific settings. Never committed.

Everything is validated by pydantic at startup, so a typo in `config.yaml`
fails loudly with a readable message instead of misbehaving later.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]

GICS_SECTORS: tuple[str, ...] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)

# Research agents that exist once. The Sector Analyst additionally runs once per GICS sector.
SINGLETON_AGENTS: tuple[str, ...] = (
    "macro",
    "earnings",
    "sec_filings",
    "news_event",
    "technicals",
    "insider_institutional",
    "contrarian",
)
SECTOR_AGENT = "sector_analyst"
KNOWN_AGENTS: frozenset[str] = frozenset({*SINGLETON_AGENTS, SECTOR_AGENT})


class _Strict(BaseModel):
    """Base for config sections: unknown keys are errors (catches typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- sections


class UniverseConfig(_Strict):
    index: Literal["sp500", "sp100", "custom"] = "sp500"
    custom_tickers: tuple[str, ...] = ()
    benchmark: str = "SPY"

    @model_validator(mode="after")
    def _custom_needs_tickers(self) -> Self:
        if self.index == "custom" and not self.custom_tickers:
            raise ValueError("universe.index is 'custom' but universe.custom_tickers is empty")
        return self


class ScheduleConfig(_Strict):
    """All times are wall-clock times in `timezone`, on trading days only."""

    timezone: str = "America/New_York"
    ingest: time = time(6, 30)
    research_agents: time = time(7, 30)
    score_and_judge: time = time(8, 20)
    b_signals: time = time(8, 0)  # system B's extra signals, before its judge
    b_fable: time = time(10, 30)  # system B's Fable test (never trades, so any time works)
    b_postmortems: time = time(7, 0)  # system B: yesterday's "why did it move?" cases
    morning_brief: time = time(8, 45)
    place_orders: time = time(9, 35)
    monitor_interval_minutes: int = Field(default=10, ge=1, le=60)
    reconcile_eod: time = time(16, 15)
    resolve_outcomes: time = time(17, 0)
    weekly_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sat"
    weekly_time: time = time(9, 0)
    heartbeat_grace_minutes: int = Field(default=15, ge=1)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if unknown
        return value

    @model_validator(mode="after")
    def _orders_after_open(self) -> Self:
        if self.place_orders < time(9, 35):
            raise ValueError(
                "schedule.place_orders must be 09:35 or later (spec: orders after 9:35 ET)"
            )
        ordered = [self.ingest, self.research_agents, self.score_and_judge, self.morning_brief,
                   self.place_orders, self.reconcile_eod, self.resolve_outcomes]  # fmt: skip
        if ordered != sorted(ordered):
            raise ValueError("schedule times must be in chronological order through the day")
        return self


class ModelPrice(_Strict):
    """USD per million tokens. Batch requests are billed at `batch_discount` of these."""

    input: float = Field(gt=0)
    output: float = Field(gt=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class BudgetConfig(_Strict):
    monthly_usd: float = Field(default=100.0, gt=0)
    # Fractions of the monthly budget at which the cost governor reacts.
    warn_at: Fraction = 0.70
    degrade_at: Fraction = 0.85
    hard_stop_at: Fraction = 1.0
    # A single day may spend at most this multiple of (monthly budget / trading days left).
    daily_overspend_factor: float = Field(default=1.5, ge=1.0)
    batch_discount: Fraction = 0.5
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> Self:
        if not self.warn_at <= self.degrade_at <= self.hard_stop_at:
            raise ValueError("budget thresholds must satisfy warn_at <= degrade_at <= hard_stop_at")
        return self


# The Anthropic SDK refuses a non-streaming request that could run longer than ten
# minutes; that limit works out at about 21,000 output tokens.
MAX_OUTPUT_TOKENS = 21_000


class AgentSettings(_Strict):
    model: str = "claude-sonnet-5-5"
    effort: Effort = "medium"
    max_tokens: int = Field(default=16000, ge=256, le=MAX_OUTPUT_TOKENS)
    enabled: bool = True


class AgentOverride(_Strict):
    """Per-agent overrides; anything left out falls back to `agents.defaults`."""

    model: str | None = None
    effort: Effort | None = None
    max_tokens: int | None = Field(default=None, ge=256)
    enabled: bool | None = None


class AgentsConfig(_Strict):
    defaults: AgentSettings = AgentSettings()
    concurrency: int = Field(default=5, ge=1, le=32)
    # Max tickers each agent may report on per cycle (keeps cost bounded).
    max_reports_per_agent: int = Field(default=5, ge=1)
    # How far back "fresh" news and filings reach (three days more on a Monday).
    lookback_hours: int = Field(default=26, ge=1)
    # Upper limits on what is put in front of one agent, to bound the cost of a call.
    max_news_items: int = Field(default=250, ge=10)
    max_tickers_in_context: int = Field(default=40, ge=5)
    # How many of the other agents' ideas the contrarian tries to knock down.
    contrarian_targets: int = Field(default=8, ge=1)
    overrides: dict[str, AgentOverride] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _known_agents_only(cls, value: dict[str, AgentOverride]) -> dict[str, AgentOverride]:
        unknown = sorted(set(value) - KNOWN_AGENTS)
        if unknown:
            raise ValueError(
                f"agents.overrides has unknown agent(s) {unknown}; known: {sorted(KNOWN_AGENTS)}"
            )
        return value

    def for_agent(self, name: str) -> AgentSettings:
        """Effective settings for one agent (defaults merged with its override)."""
        if name not in KNOWN_AGENTS:
            raise KeyError(f"unknown agent {name!r}")
        override = self.overrides.get(name)
        if override is None:
            return self.defaults
        return self.defaults.model_copy(update=override.model_dump(exclude_none=True))


class BlendWeights(_Strict):
    """final_confidence = judge*J + model*M + agreement*A  (weights sum to 1)."""

    judge: Fraction = 0.5
    model: Fraction = 0.3
    agreement: Fraction = 0.2

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = self.judge + self.model + self.agreement
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"judge.blend weights must sum to 1.0 (got {total:.3f})")
        return self


class JudgeConfig(_Strict):
    model: str = "claude-opus-5-5"
    effort: Effort = "high"
    max_tokens: int = Field(default=20000, ge=1024, le=MAX_OUTPUT_TOKENS)
    confidence_threshold: Fraction = 0.65
    max_trades_per_day: int = Field(default=5, ge=1)
    blend: BlendWeights = BlendWeights()


class RiskConfig(_Strict):
    """Risk limits. There is no leverage or options setting on purpose: neither is ever allowed."""

    max_position_pct: Fraction = 0.10
    max_open_positions: int = Field(default=10, ge=1)
    max_sector_pct: Fraction = 0.30
    allow_shorts: bool = False
    daily_loss_kill_switch_pct: Fraction = 0.03

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.max_position_pct <= 0 or self.max_sector_pct <= 0:
            raise ValueError("risk percentages must be greater than 0")
        if self.max_position_pct > self.max_sector_pct:
            raise ValueError("risk.max_position_pct cannot exceed risk.max_sector_pct")
        if self.max_position_pct * self.max_open_positions > 1.0 + 1e-9:
            raise ValueError(
                "risk.max_position_pct x risk.max_open_positions exceeds 100% of equity; "
                "that would require leverage, which is not allowed"
            )
        return self


class TradingConfig(_Strict):
    order_type: Literal["market", "limit"] = "limit"
    # For limit entries: how far above the last price a buy limit may be placed.
    limit_slippage_pct: Fraction = 0.003
    # Bracket orders: the take-profit and stop-loss legs share the entry's time in force.
    # With "day" they would be cancelled at the close, leaving a multi-day position
    # unprotected overnight, so "gtc" is the default. Entries still unfilled at the end
    # of the day are cancelled by the end-of-day reconcile.
    time_in_force: Literal["day", "gtc"] = "gtc"
    # Real-time prices on the free Alpaca data plan come from the IEX feed only.
    live_feed: Literal["iex", "sip", "delayed_sip"] = "iex"
    # The time stop sells this many minutes before the close of the last horizon day.
    time_stop_minutes_before_close: int = Field(default=15, ge=5, le=120)
    # Thesis break: a held stock gets fresh news this negative (-1..1 word-list score).
    thesis_break_sentiment: float = Field(default=-0.3, ge=-1.0, le=0.0)
    thesis_review_cooldown_hours: int = Field(default=6, ge=1)
    # The judge must be at least this sure that the thesis is broken to sell.
    thesis_exit_confidence: Fraction = 0.6


class PatternModelConfig(_Strict):
    horizons_days: tuple[int, ...] = (1, 3, 5, 10)
    primary_horizon_days: int = 5
    walk_forward_splits: int = Field(default=6, ge=2)
    embargo_days: int = Field(default=10, ge=0)
    calibration: Literal["isotonic", "platt"] = "isotonic"
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)
    # The backtest's trading rule: each day buy up to `backtest_top_n` candidates whose
    # calibrated probability is at least `backtest_threshold`.
    backtest_threshold: Fraction = 0.55
    backtest_top_n: int = Field(default=3, ge=1)
    # Claude model that reads MASKED historical headlines (Batches API, half price).
    text_model: str = "claude-haiku-5-5"
    text_effort: Effort = "low"
    text_items_per_request: int = Field(default=40, ge=1, le=200)

    @model_validator(mode="after")
    def _primary_in_horizons(self) -> Self:
        if self.primary_horizon_days not in self.horizons_days:
            raise ValueError("pattern_model.primary_horizon_days must be one of horizons_days")
        return self


class BackfillConfig(_Strict):
    years: int = Field(default=5, ge=1, le=12)


class FredSeries(_Strict):
    id: str
    # True for series whose past values get revised (CPI, payrolls...). For those the
    # full revision history is stored so a past decision only sees what was known then.
    revised: bool = False


class Manager13F(_Strict):
    cik: str
    name: str

    @field_validator("cik")
    @classmethod
    def _digits(cls, value: str) -> str:
        if not value.isdigit() or len(value) > 10:
            raise ValueError(f"13F manager cik must be up to 10 digits, got {value!r}")
        return value.zfill(10)


class IngestConfig(_Strict):
    # sip = all US exchanges (works on the free Alpaca plan for data older than 15 minutes);
    # iex = one exchange only (smaller volumes).
    alpaca_feed: Literal["sip", "iex", "delayed_sip"] = "sip"
    # Prices adjusted for splits and dividends, so returns across those dates are right.
    bar_adjustment: Literal["raw", "split", "dividend", "all"] = "all"
    finnhub_news: bool = True
    news_dedupe_window_hours: int = Field(default=72, ge=1)
    sec_requests_per_second: float = Field(default=8.0, gt=0, le=10)  # SEC allows at most 10
    form4_detail_years: int = Field(default=2, ge=0, le=12)
    sector_etfs: tuple[str, ...] = ()
    fred_series: tuple[FredSeries, ...] = ()
    managers_13f: tuple[Manager13F, ...] = ()


class TelegramConfig(_Strict):
    enabled: bool = True
    # Messages that could not be sent within this many hours are dropped, not sent late.
    max_age_hours: int = Field(default=24, ge=1)
    outbox_interval_seconds: int = Field(default=15, ge=5)
    poll_timeout_seconds: int = Field(default=25, ge=1, le=50)
    max_attempts: int = Field(default=5, ge=1)
    brief_max_trades: int = Field(default=5, ge=1, le=10)


class LearningConfig(_Strict):
    """How past agent calls are scored and turned into weights and lessons."""

    resolve_max_age_days: int = Field(default=60, ge=10)
    stats_windows_days: list[int] = [30, 90, 365]
    weight_window_days: int = 90
    weight_min_calls: int = Field(default=10, ge=1)
    weight_prior_calls: int = Field(default=30, ge=0)
    weight_scale: float = Field(default=4.0, gt=0, le=10)
    weight_min: float = Field(default=0.25, gt=0, le=1)
    weight_max: float = Field(default=2.0, ge=1, le=5)
    lessons_model: str | None = None  # default: the judge's model
    lessons_effort: Effort = "medium"
    lessons_max_tokens: int = Field(default=8000, ge=1000)
    lessons_lookback_days: int = Field(default=28, ge=7)
    lessons_min_outcomes: int = Field(default=10, ge=1)
    lessons_max_age_days: int = Field(default=21, ge=7)
    text_batch_limit: int = Field(default=3000, ge=0)

    @model_validator(mode="after")
    def _weight_window_is_computed(self) -> Self:
        if self.weight_window_days not in self.stats_windows_days:
            raise ValueError(
                "learning.weight_window_days must be one of learning.stats_windows_days"
            )
        return self


# --------------------------------------------------------------------------- system B


class ConfidenceSwitch(_Strict):
    """1. Confidence: leave the pattern model out, calibrate, and abstain when unsure."""

    use_pattern_model: bool = True
    calibrate: bool = False  # isotonic regression on ideas whose outcome is known
    abstain: bool = False  # conformal prediction: skip trades the data cannot vouch for
    calibrated_threshold: Fraction = (
        0.55  # bar for the calibrated probability of beating the market
    )
    min_ideas: int = Field(default=60, ge=20)  # resolved ideas needed before calibrating
    abstain_error_rate: Fraction = 0.2  # conformal alpha: how often the trade set may be wrong
    window_days: int = Field(default=365, ge=30)


class RiskSwitch(_Strict):
    """2. Sizing by volatility, stops and targets from ATR, and the SPY 200-day filter."""

    vol_sizing: bool = False
    risk_per_trade_pct: Fraction = 0.01  # share of equity lost if the stop is hit
    atr_brackets: bool = False
    atr_stop_multiple: float = Field(default=2.0, gt=0, le=10)
    atr_target_multiple: float = Field(default=3.0, gt=0, le=20)
    spy_trend_filter: Literal["off", "halve", "block"] = (
        "off"  # when SPY is below its 200-day average
    )
    trend_days: int = Field(default=200, ge=20)


class VetoSwitch(_Strict):
    """3. A second model reads each trade and may only remove it or make it smaller."""

    enabled: bool = False
    model: str = "claude-sonnet-5-5"
    effort: Effort = "medium"
    max_tokens: int = Field(default=12000, ge=1024, le=MAX_OUTPUT_TOKENS)
    shrink_to: Fraction = 0.5  # a "shrink" verdict multiplies the size by this


class PruningSwitch(_Strict):
    """4. Leave out agents whose track record, after shrinkage, is worse than a coin flip."""

    enabled: bool = False
    min_calls: int = Field(default=20, ge=5)
    prior_calls: int = Field(default=30, ge=0)  # shrinks small samples toward 50%
    drop_below: Fraction = 0.48  # shrunk hit rate below this: the agent is left out


class InsiderSwitch(_Strict):
    """5. Insider cluster buying from Form 4 (backtest it first: `backtest insiders`)."""

    enabled: bool = False
    min_buyers: int = Field(default=3, ge=2)
    window_days: int = Field(default=30, ge=5, le=120)
    min_usd: float = Field(default=100_000, ge=0)
    confidence_bonus: float = Field(
        default=0.0, ge=0, le=0.05
    )  # added to backed trades, 0 = context only


class SectorSwitch(_Strict):
    """6. Judge stocks against their sector ETF, not only against SPY."""

    enabled: bool = False


class CostBandSwitch(_Strict):
    """7. Skip trades whose expected edge does not clearly beat their costs."""

    enabled: bool = False
    round_trip_bps: float = Field(default=10.0, ge=0)  # spread + slippage, in and out
    margin: float = Field(default=2.0, ge=1)  # expected edge must be this many times the costs


class OptionsSwitch(_Strict):
    """8. Options data (MarketData.app, paid): implied volatility, skew, call-put spread."""

    enabled: bool = False
    max_tickers: int = Field(default=15, ge=1, le=100)


class AnalystSwitch(_Strict):
    """9. Analyst grades, price targets and estimate revisions (FMP, paid)."""

    enabled: bool = False
    max_tickers: int = Field(default=15, ge=1, le=100)


class MetaLabelSwitch(_Strict):
    """10. Meta-labelling: a small model trained on past ideas decides which to take."""

    enabled: bool = False
    min_ideas: int = Field(default=150, ge=50)
    threshold: Fraction = 0.5


class FableShadow(_Strict):
    """The Fable test: some agents also run on Claude Fable, for comparison only."""

    enabled: bool = False
    model: str = "claude-fable-5-2"
    compare_with: str = "claude-sonnet-5-5"
    agents: tuple[str, ...] = ("news_event", "earnings")
    effort: Effort = "high"
    max_tokens: int = Field(default=20000, ge=4096, le=MAX_OUTPUT_TOKENS)
    monthly_usd: float = Field(default=10.0, gt=0)

    @field_validator("agents")
    @classmethod
    def _known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - KNOWN_AGENTS)
        if unknown:
            raise ValueError(f"unknown agent(s) {unknown}; known: {sorted(KNOWN_AGENTS)}")
        return value


class IntradaySwitch(_Strict):
    """X2b. The intraday scanner: checks the market every few minutes for set-ups, and
    announces each planned trade on Telegram before placing it (send /skip to cancel)."""

    enabled: bool = False
    mode: Literal["shadow", "live"] = (
        "shadow"  # shadow: everything but the order, scored afterwards
    )
    interval_minutes: int = Field(default=5, ge=1, le=30)
    start: time = time(9, 45)
    end: time = time(15, 30)
    candidates_per_scan: int = Field(default=6, ge=1, le=20)
    max_calls_per_day: int = Field(default=30, ge=0, le=200)  # Claude calls, screening and deciding
    max_decisions_per_day: int = Field(default=8, ge=0, le=50)  # the more expensive model's calls
    max_trades_per_day: int = Field(default=3, ge=0, le=20)
    monthly_usd: float = Field(default=10.0, gt=0)  # the scanner's share of B's Claude budget
    skip_window_minutes: int = Field(default=5, ge=1, le=60)
    screen_model: str = "claude-haiku-5-5"
    decide_model: str = "claude-sonnet-5-5"
    decide_effort: Effort = "medium"
    min_confidence: Fraction = 0.6
    min_reward_risk: float = Field(default=1.5, ge=1.0)
    max_hold_days: int = Field(default=3, ge=1, le=10)
    min_price: float = Field(default=5.0, ge=0)
    min_relative_volume: float = Field(default=1.5, ge=0)
    big_move_atr: float = Field(default=1.0, gt=0)  # a day's move this many average daily ranges
    cooldown_minutes: int = Field(
        default=60, ge=0
    )  # do not look at the same stock again before this
    cost_bps: float = Field(default=10.0, ge=0)  # round trip, taken off every shadow result

    @model_validator(mode="after")
    def _window(self) -> Self:
        if not time(9, 35) <= self.start < self.end <= time(15, 50):
            raise ValueError(
                "decision.intraday: start/end must lie between 09:35 and 15:50, start first"
            )
        return self


class PostMortemSwitch(_Strict):
    """X2c. "Why did it move?" post-mortems, and careful lessons for the judge."""

    enabled: bool = False
    max_cases_per_day: int = Field(default=10, ge=1, le=30)
    case_model: str = "claude-haiku-5-5"
    summary_model: str = "claude-sonnet-5-5"
    big_move_ranges: float = Field(
        default=2.0, gt=0
    )  # a move this many daily ranges beyond the sector
    lookback_days: int = Field(
        default=5, ge=1, le=20
    )  # ideas this recent are checked for missed chances
    apply_to_judge: bool = False  # show similar past cases to the judge, with capped adjustments
    max_similar: int = Field(default=3, ge=1, le=10)
    cap_anecdote: float = Field(default=0.03, ge=0, le=0.05)  # also for "emerging"
    cap_supported: float = Field(default=0.07, ge=0, le=0.10)
    case_max_age_days: int = Field(default=365, ge=30)
    monthly_usd: float = Field(default=5.0, gt=0)  # their share of B's Claude budget


class DecisionConfig(_Strict):
    """System B's decision layer. Everything off is exactly system A's behaviour."""

    confidence: ConfidenceSwitch = ConfidenceSwitch()
    risk: RiskSwitch = RiskSwitch()
    veto: VetoSwitch = VetoSwitch()
    pruning: PruningSwitch = PruningSwitch()
    insiders: InsiderSwitch = InsiderSwitch()
    sector_relative: SectorSwitch = SectorSwitch()
    cost_band: CostBandSwitch = CostBandSwitch()
    options: OptionsSwitch = OptionsSwitch()
    analysts: AnalystSwitch = AnalystSwitch()
    meta_label: MetaLabelSwitch = MetaLabelSwitch()
    fable_shadow: FableShadow = FableShadow()
    intraday: IntradaySwitch = IntradaySwitch()
    postmortems: PostMortemSwitch = PostMortemSwitch()

    @property
    def active(self) -> bool:
        return self != DecisionConfig()

    def switches(self) -> dict[str, bool]:
        """Which of the ten switches are on (for logs, the brief and the decision log)."""
        c, r = self.confidence, self.risk
        return {
            "1_confidence": (not c.use_pattern_model) or c.calibrate or c.abstain,
            "2_risk": r.vol_sizing or r.atr_brackets or r.spy_trend_filter != "off",
            "3_veto": self.veto.enabled,
            "4_pruning": self.pruning.enabled,
            "5_insiders": self.insiders.enabled,
            "6_sector_relative": self.sector_relative.enabled,
            "7_cost_band": self.cost_band.enabled,
            "8_options": self.options.enabled,
            "9_analysts": self.analysts.enabled,
            "10_meta_label": self.meta_label.enabled,
        }


# --------------------------------------------------------------------------- system C

FundGroup = Literal["equity", "bonds", "commodity", "currency", "cash", "managed_futures", "crypto"]


class FundSpec(_Strict):
    """One ETF of system C's universe."""

    ticker: str
    group: FundGroup
    cap: Fraction = 0.20  # most of the portfolio this fund may take
    avanza: str = ""  # the European (UCITS) product to copy it with on Avanza, "" = none
    stand_in: str | None = None  # older fund used in backtests before this one existed


class CUpgrades(_Strict):
    """The five upgrades, each tested in the backtest before it is switched on."""

    cash_in_tbills: bool = False  # 1: idle money in a T-bill fund instead of cash
    dual_momentum: bool = False  # 2: a fund must also beat T-bills over 12 months
    risk_parity: bool = False  # 3: weights from the correlations, not only each fund's volatility
    managed_futures: bool = False  # 4: DBMF / KMLM in the universe
    fractional: bool = False  # 5: fractional shares (for small accounts)


class CVeto(_Strict):
    """Claude's only roles in C: a weekly risk-reducing review and a daily news watch."""

    enabled: bool = False
    model: str = "claude-sonnet-5-5"
    effort: Effort = "medium"
    watch_enabled: bool = False
    watch_model: str = "claude-haiku-5-5"


class CSleeve(_Strict):
    """C's daily chart trades: a separate part with its own switch (backtest first)."""

    enabled: bool = False
    mode: Literal["shadow", "live"] = "shadow"
    max_share: Fraction = 0.20  # of C's money
    per_trade: Fraction = 0.05
    breakout_days: int = Field(default=20, ge=5)
    dip_rsi: float = Field(default=10.0, ge=1, le=50)  # RSI(2) below this, in an uptrend
    hold_days: int = Field(default=7, ge=1, le=30)
    stop_atr: float = Field(default=2.0, gt=0)
    target_atr: float = Field(default=3.0, gt=0)


class PortfolioConfig(_Strict):
    """System C: the weekly trend-following ETF portfolio (docs/EXPERIMENT.md)."""

    funds: tuple[FundSpec, ...] = ()
    cash_fund: str = "SGOV"
    top_n: int = Field(default=8, ge=1, le=30)
    score_days: tuple[int, ...] = (63, 126, 252)  # momentum score: average return over these
    check_days: tuple[int, ...] = (21, 63, 126, 252)  # eligible when most of these are positive
    min_positive: int = Field(default=3, ge=1)
    trend_days: int = Field(default=200, ge=20)
    vol_days: int = Field(default=63, ge=20)
    vol_target: Fraction = 0.10  # yearly volatility the portfolio aims for (never leveraged)
    group_caps: dict[str, float] = Field(default_factory=lambda: {
        "equity": 0.60, "commodity": 0.30, "crypto": 0.10, "managed_futures": 0.15})  # fmt: skip
    drift_band: Fraction = 0.20  # trade a fund only when its weight is this far (relatively) off
    min_trade_weight: Fraction = 0.01
    drawdown_halve: Fraction = 0.10
    drawdown_flat: Fraction = 0.15
    flat_weeks: int = Field(default=4, ge=1)  # after going flat, wait this long, then start again
    emergency_stop_pct: Fraction = 0.20  # a resting stop this far below every holding
    limit_slippage_pct: Fraction = 0.003
    cost_bps: float = Field(default=5.0, ge=0)  # per side, in backtests
    upgrades: CUpgrades = CUpgrades()
    veto: CVeto = CVeto()
    sleeve: CSleeve = CSleeve()
    plan_day: Literal["mon", "tue", "wed", "thu", "fri"] = "fri"
    plan_time: time = time(16, 45)  # after the close: signals, review, the weekly plan
    data_time: time = time(16, 30)
    rebalance_time: time = time(9, 35)
    watch_time: time = time(8, 0)
    mirror_capital_usd: float = Field(default=1000.0, gt=0)  # the Avanza amounts are shown for this
    sek_per_usd: float = Field(default=10.5, gt=0)  # approximate; update now and then

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.min_positive > len(self.check_days):
            raise ValueError("portfolio.min_positive cannot exceed the number of check_days")
        if not self.drawdown_halve < self.drawdown_flat:
            raise ValueError("portfolio.drawdown_halve must be smaller than drawdown_flat")
        return self

    def by_ticker(self) -> dict[str, FundSpec]:
        return {f.ticker: f for f in self.funds}


class ExperimentConfig(_Strict):
    """X3. How the three-way experiment is measured (docs/PREREGISTRATION.md)."""

    real_capital_usd: float = Field(default=1000.0, gt=0)  # results are also shown at this size
    data_costs_usd: dict[str, float] = Field(
        default_factory=dict
    )  # monthly paid data per system, e.g. {"B": 12}
    pilot_days: int = Field(default=21, ge=1)
    test_days: int = Field(default=63, ge=21)
    weekly_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sat"
    weekly_time: time = time(10, 30)


class AppConfig(_Strict):
    universe: UniverseConfig = UniverseConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    budget: BudgetConfig = BudgetConfig()
    agents: AgentsConfig = AgentsConfig()
    judge: JudgeConfig = JudgeConfig()
    risk: RiskConfig = RiskConfig()
    trading: TradingConfig = TradingConfig()
    pattern_model: PatternModelConfig = PatternModelConfig()
    backfill: BackfillConfig = BackfillConfig()
    ingest: IngestConfig = IngestConfig()
    telegram: TelegramConfig = TelegramConfig()
    learning: LearningConfig = LearningConfig()
    decision: DecisionConfig = DecisionConfig()  # system B only
    experiment: ExperimentConfig = ExperimentConfig()  # the hub and the comparison
    portfolio: PortfolioConfig = PortfolioConfig()  # system C only

    def models_in_use(self) -> set[str]:
        models = {self.judge.model, self.pattern_model.text_model}
        if self.decision.veto.enabled:
            models.add(self.decision.veto.model)
        if self.decision.fable_shadow.enabled:
            models.add(self.decision.fable_shadow.model)
        if self.decision.intraday.enabled:
            models |= {self.decision.intraday.screen_model, self.decision.intraday.decide_model}
        if self.decision.postmortems.enabled:
            models |= {
                self.decision.postmortems.case_model,
                self.decision.postmortems.summary_model,
            }
        for name in KNOWN_AGENTS:
            settings = self.agents.for_agent(name)
            if settings.enabled:
                models.add(settings.model)
        return models

    @model_validator(mode="after")
    def _every_model_is_priced(self) -> Self:
        # The cost governor cannot enforce a budget for a model it has no price for.
        missing = sorted(self.models_in_use() - set(self.budget.prices))
        if missing:
            raise ValueError(
                f"budget.prices has no entry for model(s) {missing}; add their "
                "USD-per-million-token prices so API spend can be tracked"
            )
        return self


# --------------------------------------------------------------------------- secrets / env


class Settings(BaseSettings):
    """Secrets and machine-specific settings, read from the environment / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    # The experiment: which system this process runs, and the paper accounts of B and C.
    system: Literal["A", "B", "C"] = "A"
    alpaca_b_api_key: SecretStr | None = None
    alpaca_b_secret_key: SecretStr | None = None
    alpaca_c_api_key: SecretStr | None = None
    alpaca_c_secret_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    finnhub_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    marketdata_api_key: SecretStr | None = None  # system B, switch 8 (paid)
    fmp_api_key: SecretStr | None = None  # system B, switch 9 (paid)
    tiingo_api_key: SecretStr | None = None  # system C's 20-year backtest (free)
    sec_user_agent: str | None = None
    wikipedia_contact: str | None = None

    database_url: str = "sqlite:///data/papertrader.db"
    papertrader_config: Path = Path("config.yaml")
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    backups_dir: Path | None = None  # where the backup service writes (read-only to the app)
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_default(cls, value: Any, info: Any) -> Any:
        # An empty line like `FINNHUB_API_KEY=` in .env means "not provided".
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    def alpaca_keys(self, system: str | None = None) -> tuple[SecretStr | None, SecretStr | None]:
        """(API key, secret key) of a system's own Alpaca paper account."""
        name = (system or self.system).upper()
        if name == "B":
            return self.alpaca_b_api_key, self.alpaca_b_secret_key
        if name == "C":
            return self.alpaca_c_api_key, self.alpaca_c_secret_key
        return self.alpaca_api_key, self.alpaca_secret_key

    def secret_status(self) -> dict[str, bool]:
        """Which keys are present - names only, never values. Safe to log."""
        names = (
            "anthropic_api_key",
            "alpaca_api_key",
            "alpaca_secret_key",
            "alpaca_b_api_key",
            "alpaca_b_secret_key",
            "alpaca_c_api_key",
            "alpaca_c_secret_key",
            "telegram_bot_token",
            "telegram_chat_id",
            "finnhub_api_key",
            "fred_api_key",
            "marketdata_api_key",
            "fmp_api_key",
            "tiingo_api_key",
            "sec_user_agent",
        )
        return {name.upper(): getattr(self, name) is not None for name in names}


# --------------------------------------------------------------------------- loading


class ConfigError(RuntimeError):
    """A set-up problem a person must fix: config.yaml missing or invalid, database not set up."""


def load_config(path: Path | str) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path.resolve()}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config(get_settings().papertrader_config)
