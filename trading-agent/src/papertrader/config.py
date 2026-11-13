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


class AgentSettings(_Strict):
    model: str = "claude-sonnet-5-5"
    effort: Effort = "medium"
    max_tokens: int = Field(default=16000, ge=256)
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
    max_tokens: int = Field(default=32000, ge=1024)
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
    time_in_force: Literal["day", "gtc"] = "day"


class PatternModelConfig(_Strict):
    horizons_days: tuple[int, ...] = (1, 3, 5, 10)
    primary_horizon_days: int = 5
    walk_forward_splits: int = Field(default=6, ge=2)
    embargo_days: int = Field(default=10, ge=0)
    calibration: Literal["isotonic", "platt"] = "isotonic"
    transaction_cost_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)

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

    def models_in_use(self) -> set[str]:
        models = {self.judge.model}
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
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    finnhub_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    sec_user_agent: str | None = None

    database_url: str = "sqlite:///data/papertrader.db"
    papertrader_config: Path = Path("config.yaml")
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_default(cls, value: Any, info: Any) -> Any:
        # An empty line like `FINNHUB_API_KEY=` in .env means "not provided".
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    def secret_status(self) -> dict[str, bool]:
        """Which keys are present - names only, never values. Safe to log."""
        names = (
            "anthropic_api_key",
            "alpaca_api_key",
            "alpaca_secret_key",
            "telegram_bot_token",
            "telegram_chat_id",
            "finnhub_api_key",
            "fred_api_key",
            "sec_user_agent",
        )
        return {name.upper(): getattr(self, name) is not None for name in names}


# --------------------------------------------------------------------------- loading


class ConfigError(RuntimeError):
    """config.yaml is missing or invalid."""


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
