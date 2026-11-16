from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from papertrader.cli import app
from papertrader.config import PatternModelConfig
from papertrader.db import models as m
from papertrader.db.session import make_engine, make_session_factory
from papertrader.ingest.timeutil import at_new_york
from papertrader.model.backtest import price_paths, run_backtest, simulate, stats
from papertrader.model.dataset import build_dataset
from papertrader.model.features import FEATURES, assemble, assign_entry_day, price_features
from papertrader.model.pipeline import NotEnoughDataError, run_training
from papertrader.model.registry import active_version
from papertrader.model.report import sanity_warnings, to_html
from papertrader.model.score import score_live
from papertrader.model.text import lexicon_sentiment, mask_text, name_variants, rule_event_type
from papertrader.model.train import (
    calibration_table,
    daily_ic,
    fit,
    metrics,
    walk_forward,
    walk_forward_folds,
)

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from .helpers import business_days, store_market, synth_market

CFG = shipped_config()
PM = CFG.pattern_model
DECISION = time(8, 20)


def dataset(effect: float, **kwargs):  # type: ignore[no-untyped-def]
    bars, events, sectors = synth_market(effect=effect, **kwargs)
    table = assemble(bars=bars, events=events, sectors=sectors, benchmark="SPY",
                     horizons=PM.horizons_days, decision_time=DECISION)  # fmt: skip
    return bars, events, sectors, table


# ----------------------------------------------------------------------------- text


def test_masking_hides_who_and_when() -> None:
    text = ("Apple Inc. (NASDAQ: AAPL) beats Q3 2023 estimates; AAPL jumps after Microsoft deal on "
            "November 2, 2023. Apple's CEO sees a strong fiscal 2024.")  # fmt: skip
    masked = mask_text(
        text, own=[("AAPL", "Apple Inc.")], others=[("MSFT", "Microsoft Corporation")]
    )
    assert masked == ("[COMPANY] beats [DATE] estimates; [COMPANY] jumps after [OTHER] deal on [DATE]. "
                      "[COMPANY] CEO sees a strong [DATE].")  # fmt: skip
    for secret in ("Apple", "AAPL", "Microsoft", "2023", "2024", "November", "Q3", "NASDAQ"):
        assert secret not in masked


def test_masking_does_not_eat_ordinary_words() -> None:
    masked = mask_text("Agilent and Allstate rally to ALL-time highs; A new plan for all",
                       own=[("A", "Agilent Technologies")], others=[("ALL", "Allstate Corp")])  # fmt: skip
    assert masked == "[COMPANY] and [OTHER] rally to [OTHER]-time highs; A new plan for all"
    assert name_variants("General Electric Company") == [
        "General Electric Company",
        "General Electric",
    ]


def test_rule_based_text_features_need_no_model() -> None:
    assert rule_event_type("Pfizer to acquire Seagen for $43 billion") == "mna"
    assert rule_event_type("FDA approval for new drug") == "fda_regulatory"
    assert rule_event_type("CEO resigns unexpectedly") == "management_change"
    assert rule_event_type("Nothing in particular happened") == "other"
    assert lexicon_sentiment("Shares surge after record profit beats estimates") > 0.5
    assert lexicon_sentiment("Shares plunge after weak results and a lawsuit") < -0.5
    assert lexicon_sentiment("The company held a meeting") == 0.0


# ----------------------------------------------------------------------------- timing / leakage


def test_events_are_assigned_to_the_first_decision_after_them() -> None:
    days = business_days(date(2026, 11, 9), 6)  # Mon 9th .. Mon 16th
    times = pd.Series([
        at_new_york(date(2026, 11, 10), time(8, 19)),  # Tuesday, just before the decision
        at_new_york(date(2026, 11, 10), time(8, 21)),  # Tuesday, just after: too late for Tuesday
        at_new_york(date(2026, 11, 13), time(17, 0)),  # Friday evening
        at_new_york(date(2026, 11, 15), time(12, 0)),  # Sunday
        at_new_york(date(2026, 11, 20), time(12, 0)),  # after the last known day
    ])  # fmt: skip
    assigned = assign_entry_day(times, days, DECISION).tolist()
    assert assigned == [
        date(2026, 11, 10),
        date(2026, 11, 11),
        date(2026, 11, 16),
        date(2026, 11, 16),
        None,
    ]


def test_features_never_change_when_the_future_changes() -> None:
    """Rewrite every price from an entry day onwards: features must stay identical."""
    bars, events, sectors, before = dataset(0.03, tickers=8, days=200)
    probe = before.iloc[len(before) // 2]
    future = bars["day"] >= probe["day"]
    changed = bars.copy()
    changed.loc[future, ["open", "high", "low", "close"]] *= 3.0
    changed.loc[future, "volume"] *= 9.0
    after = assemble(bars=changed, events=events, sectors=sectors, benchmark="SPY",
                     horizons=PM.horizons_days, decision_time=DECISION)  # fmt: skip

    same_rows = before["day"] <= probe["day"]
    numeric = [c for c in FEATURES if c != "sector"]
    pd.testing.assert_frame_equal(before.loc[same_rows, numeric].reset_index(drop=True),
                                  after.loc[after["day"] <= probe["day"], numeric].reset_index(drop=True))  # fmt: skip
    # ...while the labels, which are the future, did change.
    row_after = after[(after["day"] == probe["day"]) & (after["ticker"] == probe["ticker"])].iloc[0]
    assert row_after["ret_5d"] == pytest.approx(probe["ret_5d"])  # same factor on entry and exit
    earlier = before[before["day"] == bars.loc[~future, "day"].max()]
    moved = after[after["day"] == earlier["day"].iloc[0]]
    assert not np.allclose(earlier["ret_5d"].to_numpy(), moved["ret_5d"].to_numpy())


def test_labels_run_from_the_entry_open_to_the_exit_close() -> None:
    bars, _events, _sectors, table = dataset(0.0, tickers=4, days=120)
    row = table.iloc[40]
    own = bars[bars["ticker"] == row["ticker"]].set_index("day").sort_index()
    spy = bars[bars["ticker"] == "SPY"].set_index("day").sort_index()
    position = own.index.get_loc(row["day"])
    expected = own["close"].iloc[position + 4] / own["open"].iloc[position] - 1
    market = spy["close"].iloc[position + 4] / spy["open"].iloc[position] - 1
    assert row["ret_5d"] == pytest.approx(expected)
    assert row["excess_5d"] == pytest.approx(expected - market)
    assert table.iloc[-1]["day"] <= own.index[-1]
    assert table["up_10d"].isna().sum() > 0  # the newest rows have no 10-day outcome yet


def test_price_features_use_only_the_past() -> None:
    bars, _events, _sectors = synth_market(tickers=3, days=150)
    table = price_features(bars, "SPY")
    truncated = price_features(bars[bars["day"] <= bars["day"].unique()[100]], "SPY")
    common = truncated.index
    pd.testing.assert_frame_equal(table.loc[common], truncated)  # later bars change nothing earlier


def test_a_model_that_ranks_nothing_has_zero_skill_not_missing_skill() -> None:
    """Found on the first full S&P 500 run: with no signal, calibration flattened to one
    probability for every stock and the report wrongly said "too little data"."""
    days = business_days(date(2025, 1, 2), 30)
    rows = [{"day": day, "prob": 0.494, "excess_5d": (i % 7) / 100 - 0.03}
            for day in days for i in range(20)]  # fmt: skip
    flat = pd.DataFrame(rows)
    assert daily_ic(flat, 5) == 0.0
    assert daily_ic(flat.head(3), 5) is None  # genuinely too few stocks on any day


def test_walk_forward_never_trains_on_the_future_and_leaves_a_gap() -> None:
    days = pd.Series(business_days(date(2024, 1, 2), 420))
    folds = walk_forward_folds(days, splits=6, embargo=10)
    assert len(folds) == 6
    unique = sorted(days.unique())
    for train_end, test_start, test_end in folds:
        assert train_end < test_start <= test_end
        assert unique.index(test_start) - unique.index(train_end) > 10  # the embargo
    starts = [f[1] for f in folds]
    assert starts == sorted(starts) and folds[-1][2] == unique[-1]
    assert len({f[1] for f in folds}) == 6  # test blocks do not overlap


# ----------------------------------------------------------------------------- learning


def test_the_model_finds_a_planted_signal_out_of_sample() -> None:
    bars, _events, _sectors, table = dataset(0.04)
    result = walk_forward(table, PM, 5)
    scored = result.predictions
    assert len(result.folds) >= 4 and scored["fold"].nunique() >= 4
    # Every scored row lies after the data its model was trained on.
    for fold in result.folds:
        assert fold["train_through"] < fold["test_from"]

    oos = metrics(scored, 5)
    assert oos["ic_daily"] > 0.15 and oos["hit_rate"] > 0.58 and oos["auc"] > 0.62
    assert oos["brier"] < oos["brier_naive"]

    backtest = run_backtest(scored, bars, benchmark="SPY", cfg=PM, risk=CFG.risk, horizon=5,
                            threshold=0.55, top_n=3)  # fmt: skip
    assert backtest["random"]["strategy_percentile"] > 0.95  # clearly better than random picks
    assert backtest["strategy"]["total_return"] > backtest["random"]["total_return_mean"]
    # A planted effect this strong is, rightly, flagged as too good to be true.
    report = {"oos": oos, "backtest": backtest}
    assert any("too good" in w for w in sanity_warnings(report))


def test_on_pure_noise_the_model_finds_nothing_and_the_report_says_so() -> None:
    bars, _events, _sectors, table = dataset(0.0)
    scored = walk_forward(table, PM, 5).predictions
    oos = metrics(scored, 5)
    assert abs(oos["ic_daily"]) < 0.06 and 0.44 < oos["hit_rate"] < 0.56
    backtest = run_backtest(scored, bars, benchmark="SPY", cfg=PM, risk=CFG.risk, horizon=5,
                            threshold=0.5, top_n=3)  # fmt: skip
    warnings = " ".join(sanity_warnings({"oos": oos, "backtest": backtest}))
    assert "too good" not in warnings
    assert "Not distinguishable from random" in warnings or "No ranking skill" in warnings


@pytest.mark.parametrize("method", ["isotonic", "platt"])
def test_calibrated_probabilities_mean_what_they_say(method: str) -> None:
    _bars, _events, _sectors, table = dataset(0.04, seed=3)
    cfg = PM.model_copy(update={"calibration": method})
    scored = walk_forward(table, cfg, 5).predictions
    assert scored["prob"].between(0, 1).all()
    bins = calibration_table(scored, 5)
    assert len(bins) == 5
    assert max(abs(b["predicted"] - b["actual"]) for b in bins) < 0.12
    assert bins[-1]["actual"] > bins[0]["actual"] + 0.2  # higher probability, more winners


def test_live_rows_match_what_training_would_have_seen() -> None:
    """Score "today" before its bar exists; later, history must show the same features."""
    bars, events, sectors = synth_market(effect=0.03, tickers=10, days=200)
    calendar = sorted(bars["day"].unique())
    today = calendar[150]
    as_of = at_new_york(today, DECISION)
    known_bars = bars[bars["day"] < today]
    known_events = type(events)(news=events.news[events.news["time"] <= as_of], earnings=events.earnings,
                                filings=events.filings, insiders=events.insiders)  # fmt: skip
    live = assemble(bars=known_bars, events=known_events, sectors=sectors, benchmark="SPY",
                    horizons=PM.horizons_days, decision_time=DECISION, as_of=as_of)  # fmt: skip
    live = live[live["day"] == today]
    history = assemble(bars=bars, events=events, sectors=sectors, benchmark="SPY",
                       horizons=PM.horizons_days, decision_time=DECISION)  # fmt: skip
    history = history[history["day"] == today]

    assert len(live) > 0 and live["ticker"].tolist() == history["ticker"].tolist()
    numeric = [c for c in FEATURES if c != "sector"]
    pd.testing.assert_frame_equal(
        live[numeric].reset_index(drop=True), history[numeric].reset_index(drop=True)
    )
    assert live["up_5d"].isna().all()  # no outcome is known yet


# ----------------------------------------------------------------------------- backtest arithmetic


def flat_bars() -> pd.DataFrame:
    days = business_days(date(2024, 1, 2), 10)
    rows = []
    for ticker, growth in (("SPY", 0.0), ("UP", 0.01)):
        price = 100.0
        for day in days:
            close = price * (1 + growth)
            rows.append({"ticker": ticker, "day": day, "open": price, "high": close, "low": price,
                         "close": close, "volume": 1.0})  # fmt: skip
            price = close
    return pd.DataFrame(rows)


def test_simulation_charges_costs_and_respects_the_position_limit() -> None:
    bars = flat_bars()
    paths = price_paths(bars, "SPY")
    days = paths.days
    picks = pd.DataFrame({"day": [days[0], days[0], days[1]], "ticker": ["UP", "UP", "UP"]})
    common = {
        "horizon": 3,
        "weight": 0.10,
        "cost_per_side": 0.001,
        "first_day": days[0],
        "last_day": days[-1],
    }

    one = simulate(picks.head(1), paths, max_positions=10, **common)
    # 10% of the account in a stock rising 1% a day for 3 days, minus 0.1% cost each way.
    assert one.iloc[0] == pytest.approx(0.10 * 0.01 - 0.10 * 0.001)
    assert one.iloc[1] == pytest.approx(0.10 * 0.01)
    assert one.iloc[2] == pytest.approx(0.10 * 0.01 - 0.10 * 0.001)
    assert one.iloc[3] == 0.0 and one.attrs["trades"] == 1

    capped = simulate(picks, paths, max_positions=2, **common)
    assert capped.attrs["trades"] == 2  # the third pick found the book full
    summary = stats(capped, weight=0.10)
    assert summary["trades"] == 2 and summary["max_drawdown"] == 0.0 and summary["total_return"] > 0
    assert stats(pd.Series([0.1, -0.5, 0.2]))["max_drawdown"] == pytest.approx(-0.5)


# ----------------------------------------------------------------------------- end to end


def trained(engine: Engine, tmp_path: Path, effect: float, when: datetime, seed: int = 1):  # type: ignore[no-untyped-def]
    return run_training(make_session_factory(engine), CFG, as_of=when, data_dir=tmp_path / "data")


def test_training_writes_a_report_with_baselines_and_a_leakage_disclaimer(
    engine: Engine, tmp_path: Path
) -> None:
    bars, events, sectors = synth_market(effect=0.02, tickers=20, days=360)
    store_market(engine, bars, events, sectors)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    result = trained(engine, tmp_path, 0.02, when)

    markdown = result.markdown_path.read_text()
    for needed in ("Read this first", "Backtests flatter", "Leakage through Claude", "Survivorship bias",
                   "Forward paper trading is the real test", "Walk-forward validation",
                   "SPY buy and hold", "Random picks", "Sharpe ratio", "Max drawdown", "Turnover per year",
                   "Information coefficient", "Hit rate", "Calibration", "basis points per side",
                   "Promoted to active: first model with measurable skill"):  # fmt: skip
        assert needed in markdown, needed
    page = result.html_path.read_text()
    assert (
        page.startswith("<!doctype html>")
        and "<table>" in page
        and "Leakage through Claude" in page
    )
    assert (
        result.dataset_path.suffix == ".parquet"
        and len(pd.read_parquet(result.dataset_path)) > 1000
    )

    sessions = make_session_factory(engine)
    with sessions() as session:
        version = active_version(session)
        assert version is not None and version.version == result.version and result.promoted
        assert version.metrics["ic_daily"] > 0 and Path(version.artifact_path).exists()
        assert version.trained_to is not None

        # Live scoring with the active model, the morning after the data ends.
        last = max(bars["day"])
        as_of = at_new_york(last + timedelta(days=1), DECISION)
        session.add(m.NewsItem(id=999_999, source="alpaca", source_item_id="live",
                               headline="S01 shares surge after record profit beats estimates",
                               published_at=as_of - timedelta(hours=2), available_at=as_of - timedelta(hours=2),
                               content_hash="f" * 64, tickers=[m.NewsTicker(ticker="S01")]))  # fmt: skip
        cycle = m.DecisionCycle(kind=m.CycleKind.DRY_RUN, as_of=as_of)
        session.add(cycle)
        session.flush()
        if as_of.astimezone(UTC).weekday() < 5:
            scores = score_live(session, CFG, as_of, cycle.id)
            assert (
                set(scores) == {"S01"} and 0.5 < scores["S01"] < 1.0
            )  # good news: above a coin flip
            stored = session.scalars(select(m.ModelScore)).one()
            assert stored.probability_up == scores["S01"] and stored.horizon_days == 5


def test_a_new_model_is_only_promoted_if_it_beats_the_active_one(
    engine: Engine, tmp_path: Path
) -> None:
    bars, events, sectors = synth_market(effect=0.03, tickers=20, days=360)
    store_market(engine, bars, events, sectors)
    first = trained(engine, tmp_path, 0.03, datetime(2026, 1, 1, tzinfo=UTC))
    assert first.promoted

    # Retraining on the same data cannot be better on unseen days than the model already active.
    second = trained(engine, tmp_path, 0.03, datetime(2026, 1, 2, tzinfo=UTC))
    assert not second.promoted and "NOT promoted" in second.report["promotion"]
    with make_session_factory(engine)() as session:
        assert active_version(session).version == first.version  # type: ignore[union-attr]
        assert len(session.scalars(select(m.ModelVersion)).all()) == 2


def test_a_model_with_no_skill_is_never_promoted_not_even_the_first(
    engine: Engine, tmp_path: Path
) -> None:
    store_market(engine, *synth_market(effect=0.0, tickers=20, days=360))
    result = trained(engine, tmp_path, 0.0, datetime(2026, 1, 1, tzinfo=UTC))
    assert not result.promoted and "NOT promoted" in result.report["promotion"]
    with make_session_factory(engine)() as session:
        assert active_version(session) is None
        assert score_live(session, CFG, datetime(2026, 1, 5, 14, tzinfo=UTC)) == {}
    # On pure noise the probabilities stay close to a coin flip instead of inventing
    # confident calls.
    assert all(abs(row["predicted"] - 0.5) < 0.10 for row in result.report["calibration"])


def test_small_calibration_samples_fall_back_to_the_smoother_method_and_stay_humble() -> None:
    _bars, _events, _sectors, table = dataset(0.0, tickers=8, days=200)
    model = fit(table, PM, 5)  # config asks for isotonic, but there are only a few hundred rows
    assert model.calibration == "platt" and model.calibration_rows < 1500
    assert 0 < model.trust < 0.8
    probabilities = model.predict(table)
    assert np.abs(probabilities - 0.5).max() <= 0.5 * model.trust + 1e-9


def test_training_refuses_politely_without_enough_data(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(NotEnoughDataError, match="ingest backfill"):
        trained(engine, tmp_path, 0.0, datetime(2026, 1, 1, tzinfo=UTC))
    with make_session_factory(engine)() as session:
        assert build_dataset(session, CFG, as_of=datetime(2026, 1, 1, tzinfo=UTC)).empty
        assert (
            score_live(session, CFG, datetime(2026, 1, 1, tzinfo=UTC)) == {}
        )  # no model: no scores


def test_fit_and_html_helpers() -> None:
    _bars, _events, _sectors, table = dataset(0.03, tickers=12, days=260)
    model = fit(table, PatternModelConfig(), 5)
    probabilities = model.predict(table.tail(50))
    assert (
        probabilities.shape == (50,) and (probabilities >= 0).all() and (probabilities <= 1).all()
    )
    html_page = to_html("# T\n\n- **bold** <b>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\ntext", "T")
    assert (
        "<h1>T</h1>" in html_page
        and "<strong>bold</strong> &lt;b&gt;" in html_page
        and "<td>2</td>" in html_page
    )


# ----------------------------------------------------------------------------- CLI

runner = CliRunner()


def test_model_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    assert "No model has been trained yet" in runner.invoke(app, ["model", "status"]).output
    assert "No report yet" in runner.invoke(app, ["model", "report"]).output
    assert "ingest backfill" in runner.invoke(app, ["model", "train"]).output

    engine = make_engine(f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    store_market(engine, *synth_market(effect=0.03, tickers=20, days=360))
    engine.dispose()
    result = runner.invoke(app, ["model", "train"])
    assert result.exit_code == 0, result.output
    assert "Promoted to active" in result.output and "random selections" in result.output
    reports = list((tmp_path / "reports").glob("*_backtest_report.html"))
    assert len(reports) == 1  # a copy lands in the easy-to-find reports folder

    assert "ACTIVE" in runner.invoke(app, ["model", "status"]).output
    assert "Read this first" in runner.invoke(app, ["model", "report"]).output
