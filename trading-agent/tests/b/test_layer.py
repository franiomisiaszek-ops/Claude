"""System B's decision layer: every switch off is A's behaviour; each switch does one thing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import Engine, select

from papertrader.agents.indicators import Bar, compute
from papertrader.agents.snapshot import Member, Snapshot
from papertrader.b.calibrate import Calibrator, fit
from papertrader.b.layer import DecisionLayer
from papertrader.b.market import Trend
from papertrader.b.models import Calibration
from papertrader.b.outcomes import IdeaSample, Resolved
from papertrader.config import ConfidenceSwitch, DecisionConfig, load_config
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.judge.schemas import RankedTrade

from ..conftest import PROJECT_ROOT
from ..research.helpers import AS_OF, FakeClaude
from .helpers import CFG, a_has_researched, b_config, b_judges, decision_log, ideas


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


def purposes(engine: Engine) -> list[str]:
    with make_session_factory(engine)() as session:
        return sorted(u.purpose for u in session.scalars(select(m.ApiUsage)))


# --------------------------------------------------------------------------- end to end


async def test_with_every_switch_off_b_decides_exactly_like_a(
    engine: Engine, b_engine: Engine
) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    await b_judges(b_engine, fake, CFG)
    log = decision_log(b_engine)
    assert set(log) == set(ideas(b_engine)) == {"AAPL", "XOM"}
    for row in log.values():
        assert (row.b_status, row.b_size, row.b_stop, row.b_target) == (
            row.a_status, row.a_size, row.a_stop, row.a_target)  # fmt: skip
        assert row.steps == [] and not any(row.switches.values())
    assert purposes(b_engine) == ["judge"]


async def test_risk_switches_and_the_review(engine: Engine, b_engine: Engine) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    fake.responses["risk_review"] = {"reviews": [
        {"ticker": "AAPL", "verdict": "shrink", "reason": "earnings inside the holding period"},
        {"ticker": "NVDA", "verdict": "veto", "reason": "not on the list: ignored"},
    ]}  # fmt: skip
    config = b_config(confidence={"use_pattern_model": False},
                      risk={"vol_sizing": True, "atr_brackets": True}, veto={"enabled": True})  # fmt: skip
    await b_judges(b_engine, fake, config)
    row, idea = decision_log(b_engine)["AAPL"], ideas(b_engine)["AAPL"]
    assert row.a_status == row.b_status == "proposed"
    daily_range = row.atr_pct * idea.entry_high / 1.005 if row.atr_pct else 0  # close x ATR%
    assert row.b_stop == pytest.approx(row.entry_reference - 2 * daily_range, abs=0.02)
    assert row.b_stop > row.a_stop  # a tighter stop from the stock's real daily range
    assert row.b_size == pytest.approx(0.05)  # risk sizing hits the 10% cap, the review halves it
    assert idea.size_pct == row.b_size and idea.stop_price == row.b_stop  # what will be traded
    assert row.veto == "shrink" and any("3 review" in s for s in row.steps)
    assert "risk_review" in fake.labels() and purposes(b_engine) == ["judge", "veto"]


async def test_a_veto_removes_a_trade_and_a_failed_review_keeps_it(
    engine: Engine, b_engine: Engine
) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    fake.responses["risk_review"] = {"reviews": [
        {"ticker": "AAPL", "verdict": "veto", "reason": "the news is a week old and priced in"},
    ]}  # fmt: skip
    await b_judges(b_engine, fake, b_config(veto={"enabled": True}))
    assert ideas(b_engine)["AAPL"].status == m.IdeaStatus.REJECTED_BY_RISK
    assert "vetoed by the review" in (ideas(b_engine)["AAPL"].status_reason or "")


async def test_a_failed_review_keeps_the_trade(engine: Engine, b_engine: Engine) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    fake.responses["risk_review"] = 500
    await b_judges(b_engine, fake, b_config(veto={"enabled": True}))
    row = decision_log(b_engine)["AAPL"]
    assert row.veto == "failed" and row.b_status == row.a_status == "proposed"


async def test_pruning_insiders_and_sector_context(engine: Engine, b_engine: Engine) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    with make_session_factory(engine)() as session:  # technicals has called 25 of 25 wrong
        old = m.DecisionCycle(kind=m.CycleKind.DAILY, as_of=AS_OF - timedelta(days=40),
                              status=m.RunStatus.SUCCEEDED)  # fmt: skip
        session.add(old)
        session.flush()
        for i in range(25):
            r = m.AgentReport(cycle_id=old.id, agent_name="technicals", as_of=old.as_of, ticker="AAPL",
                              direction=m.Direction.LONG, confidence=0.7, horizon_days=5,
                              thesis="t", evidence=[], risks=[], catalysts=[], model="x")  # fmt: skip
            session.add(r)
            session.flush()
            session.add(m.PredictionOutcome(agent_report_id=r.id, resolved_at=AS_OF - timedelta(days=30 - i),
                                            horizon_days=5, start_price=1, end_price=1, return_pct=0,
                                            benchmark_return_pct=0, excess_return_pct=-0.01, hit=False,
                                            brier=0.49))  # fmt: skip
        session.commit()
    config = b_config(pruning={"enabled": True}, insiders={"enabled": True, "min_buyers": 2},
                      sector_relative={"enabled": True})  # fmt: skip
    outcome = await b_judges(b_engine, fake, config)
    prompt = fake.calls[-1].user
    assert "technicals" not in prompt and "news_event" in prompt
    assert "Left out agents with a poor record: technicals." in outcome.details["notes"]
    assert "## Insider cluster buying" in prompt and "XOM: 2 insiders" in prompt
    assert "## Against the sector" in prompt and "AAPL vs XLK: 5 days" in prompt
    assert decision_log(b_engine)["XOM"].signals["insider_cluster"]["buyers"] == 2


async def test_calibration_learns_from_as_past_ideas(engine: Engine, b_engine: Engine) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    with make_session_factory(engine)() as session:
        for i in range(40):  # A's past ideas: confident buys worked, doubtful shorts did not
            as_of = AS_OF - timedelta(days=70 - i)
            if as_of.weekday() >= 5:
                continue
            cycle = m.DecisionCycle(
                kind=m.CycleKind.DAILY, as_of=as_of, status=m.RunStatus.SUCCEEDED
            )
            session.add(cycle)
            session.flush()
            buy = i % 2 == 0
            trade = {"ticker": "AAPL", "action": "buy" if buy else "sell_short", "horizon_days": 5,
                     "judge_confidence": 0.85 if buy else 0.55, "agent_agreement": 0.8 if buy else 0.4,
                     "status": "proposed"}  # fmt: skip
            session.add(m.JudgeDecision(cycle_id=cycle.id, as_of=as_of, market_summary="", model="x",
                                        payload={"trades": [trade]}))  # fmt: skip
        session.commit()
    config = b_config(confidence={"use_pattern_model": False, "calibrate": True, "abstain": True,
                                  "min_ideas": 20})  # fmt: skip
    outcome = await b_judges(b_engine, fake, config)
    with make_session_factory(b_engine)() as session:
        fitted = session.scalars(select(Calibration)).one()
    assert (
        fitted.active and fitted.n_total >= 20 and fitted.base_rate == pytest.approx(0.5, abs=0.1)
    )
    assert fitted.ys == sorted(fitted.ys)  # never "more confident, less likely"
    row = decision_log(b_engine)["AAPL"]
    # Today's blend lies between the past losers' and winners': the chance is read off the curve.
    assert row.calibrated == pytest.approx(
        float(np.interp(row.raw_confidence, fitted.xs, fitted.ys))
    )
    assert 0.5 < row.calibrated < 1 and row.conformal in ("trade", "abstain")
    assert any(n.startswith("Calibration: fitted on") for n in outcome.details["notes"])


def test_b_yaml_switches_on_the_defaults() -> None:
    b = load_config(PROJECT_ROOT / "config" / "b.yaml").decision
    assert b.switches() == {
        "1_confidence": True, "2_risk": True, "3_veto": True, "4_pruning": True,
        "5_insiders": False, "6_sector_relative": True, "7_cost_band": False, "8_options": False,
        "9_analysts": False, "10_meta_label": False,
    }  # fmt: skip
    assert not load_config(PROJECT_ROOT / "config" / "a.yaml").decision.active


# --------------------------------------------------------------------------- pieces


def resolved(raw: float, worked: bool, day: int) -> Resolved:
    sample = IdeaSample(AS_OF - timedelta(days=200 - day), "AAPL", "buy", 5, raw, "A")
    return Resolved(sample, 0.01 if worked else -0.01, worked)


def test_calibration_waits_for_data_then_maps_scores_to_hit_rates() -> None:
    cfg = ConfidenceSwitch(calibrate=True, abstain=True, min_ideas=60)
    few = [resolved(0.7, True, i) for i in range(30)]
    calibrator, row = fit(few, cfg, AS_OF)
    assert not calibrator.active and not row.active and calibrator.probability(0.7) == 0.7
    assert calibrator.verdict(0.9) == "waiting" and "30 of 60" in row.note

    # Scores of 0.8 worked 3 times in 4, scores of 0.5 once in 4.
    data = [resolved(0.8 if i % 2 else 0.5, (i % 8) in (1, 3, 5, 6), i) for i in range(160)]
    calibrator, row = fit(data, cfg, AS_OF)
    assert calibrator.active and row.brier_calibrated is not None
    assert calibrator.probability(0.8) == pytest.approx(0.75, abs=0.1)
    assert calibrator.probability(0.5) == pytest.approx(0.25, abs=0.1)
    assert calibrator.probability(0.95) == calibrator.probability(0.8)  # clipped at the ends
    # Neither 0.75 nor 0.25 is sure enough at a 20% error rate: abstain on both.
    assert calibrator.verdict(calibrator.probability(0.8)) == "abstain"
    assert calibrator.verdict(0.99) == "trade" if (calibrator.quantile or 1) < 0.99 else True


def test_the_conformal_rule() -> None:
    calibrator = Calibrator(True, 100, [0.0, 1.0], [0.0, 1.0], quantile=0.6)
    assert calibrator.verdict(0.7) == "trade"  # only "worked" is plausible
    assert calibrator.verdict(0.5) == "abstain"  # both are
    assert calibrator.verdict(0.3) == "abstain"  # only "failed": never a buy


def snapshot(prices: list[float]) -> Snapshot:
    start = date(2026, 1, 1)
    bars = [
        Bar(start + timedelta(days=i), p, p * 1.01, p * 0.99, p, 1e6) for i, p in enumerate(prices)
    ]
    ind = compute(bars)
    assert ind is not None
    return Snapshot(as_of=AS_OF, since=AS_OF, benchmark="SPY",
                    members={"AAPL": Member("AAPL", "Apple", "Information Technology", None)},
                    indicators={"AAPL": ind})  # fmt: skip


def trade(**changes: object) -> RankedTrade:
    base: dict[str, object] = {
        "rank": 1, "ticker": "AAPL", "action": "buy", "judge_confidence": 0.8,
        "model_probability": 0.7, "agent_agreement": 0.8, "final_confidence": 0.76,
        "entry_low": 99.5, "entry_high": 100.5, "target_price": 106.0, "stop_price": 97.0,
        "size_pct": 0.08, "horizon_days": 5, "rationale": "r", "dissent": "", "supporting": [1],
        "opposing": [], "status": "proposed",
    }  # fmt: skip
    return RankedTrade.model_validate(base | changes)


def layer(**switches: object) -> DecisionLayer:
    config = CFG.model_copy(update={"decision": DecisionConfig.model_validate(switches)})
    return DecisionLayer(config, None, None, AS_OF)  # type: ignore[arg-type]


def test_the_market_filter_halves_or_blocks_buys_below_the_200_day_average() -> None:
    snap = snapshot([100.0] * 30)
    falling = Trend("SPY", 90.0, 100.0, 200)
    halve = layer(risk={"spy_trend_filter": "halve"})
    halve.market = falling
    assert halve._score(trade(), snap, 0.65).size_pct == pytest.approx(0.04)
    block = layer(risk={"spy_trend_filter": "block"})
    block.market = falling
    blocked = block._score(trade(), snap, 0.65)
    assert blocked.status == "below_threshold" and "below its 200-day average" in (
        blocked.status_reason or ""
    )
    block.market = Trend("SPY", 110.0, 100.0, 200)
    assert block._score(trade(), snap, 0.65).status == "proposed"


def test_the_pattern_model_can_be_left_out() -> None:
    scored = layer(confidence={"use_pattern_model": False})._score(
        trade(), snapshot([100.0] * 30), 0.65
    )
    # (0.5 x 0.8 + 0.2 x 0.8) / 0.7 = 0.8: the judge and the agents only
    assert scored.final_confidence == pytest.approx(0.8) and scored.model_probability is None


def test_the_cost_band_skips_trades_with_too_small_an_edge() -> None:
    tight = trade(target_price=100.6, stop_price=99.0, entry_low=99.9, entry_high=100.1)
    out = layer(cost_band={"enabled": True, "round_trip_bps": 20})._score(
        tight, snapshot([100.0] * 30), 0.65
    )
    assert out.status == "below_threshold" and "does not clear" in (out.status_reason or "")


def test_a_refused_trade_stays_refused() -> None:
    refused = trade(status="rejected_by_risk", status_reason="no valid supporting report")
    out = layer(risk={"vol_sizing": True})._score(refused, snapshot([100.0] * 30), 0.65)
    assert out == refused
