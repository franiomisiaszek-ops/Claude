"""System B's decision layer: A's judge, then B's switches (config/b.yaml, `decision:`).

Before the judge (`prepare`):
    4  leave out agents whose record is worse than a coin flip
    5  add insider cluster buying to what the judge sees
    6  add each stock's return against its sector fund
    8  add options signals, 9 analyst signals (when their paid services are set up)

After the judge (`apply`), for every trade it proposed, in this order:
    1  the blend without the pattern model, calibrated into a probability, and
       abstain when the data cannot vouch for the trade
    5  an optional small bonus for insider clusters (0 by default: context only)
    10 meta-labelling
    2  stop and target from the stock's daily range (ATR), size from the risk at the
       stop, and the SPY 200-day filter
    7  skip trades whose expected edge does not clearly beat their costs
    3  a second model's review, which may only remove a trade or shrink it

It can re-score, resize, re-bracket or drop the judge's trades; it never adds one, and a
trade the judge's checks refused stays refused. Every step is written to b_decision_log
next to what A's rules would have done, so each switch can be measured on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.context import clip
from papertrader.agents.schemas import AgentReport
from papertrader.agents.snapshot import SECTOR_ETF, Snapshot
from papertrader.b import calibrate, meta, postmortem, pruning, signals, veto
from papertrader.b.market import Trend, relative_lines, trend
from papertrader.b.models import DecisionLog, LessonUse, PostMortem
from papertrader.b.outcomes import IdeaSample, all_samples, raw_blend, resolve
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import new_york_date
from papertrader.judge.schemas import JudgeDecision, RankedTrade
from papertrader.llm.client import ClaudeClient, LLMError
from papertrader.llm.governor import BudgetExceededError

log = logging.getLogger(__name__)

_FUNDS = frozenset(SECTOR_ETF.values())
MIN_GAP = 0.002  # stops and targets at least this far outside the entry zone


def _cents(price: float) -> float:
    return round(price, 2) if price >= 1 else round(price, 4)


class DecisionLayer:
    def __init__(self, config: AppConfig, sessions: sessionmaker[Session], engine: Engine,
                 now: datetime) -> None:  # fmt: skip
        self.config = config
        self.cfg = config.decision
        self.sessions = sessions
        self.engine = engine
        self.now = now
        self.use_pattern_model = self.cfg.confidence.use_pattern_model
        self.calibrator = calibrate.Calibrator(False, 0, note="off")
        self.meta_model = meta.MetaModel(False, "off")
        self.market: Trend | None = None
        self.pruned: list[str] = []
        self.clusters: dict[str, signals.Cluster] = {}
        self.context: dict[str, list[str]] = {}  # per ticker, also shown to the reviewer
        self._logs: dict[str, dict[str, Any]] = {}
        self._before_lessons: dict[str, float] = {}
        self._lesson_uses: list[LessonUse] = []
        self._shown: dict[int, bool] = {}  # case id -> judged comparable at least once today

    # ------------------------------------------------------------------ before the judge

    def prepare(
        self, reports: list[AgentReport], snap: Snapshot, weights: dict[str, float]
    ) -> tuple[list[AgentReport], dict[str, float], str, list[str]]:
        cfg, notes = self.cfg, []
        kept = reports
        if cfg.pruning.enabled:
            records = pruning.records(self.engine, cfg.pruning, self.now)
            self.pruned = sorted(a for a, r in records.items() if not r.keep)
            kept = [r for r in reports if r.agent_name not in self.pruned]
            if self.pruned:
                notes.append(f"Left out agents with a poor record: {', '.join(self.pruned)}.")
        tickers = sorted({r.ticker for r in kept} - _FUNDS - {snap.benchmark})
        sections: list[str] = []
        with self.sessions() as session:
            self.market = trend(session, snap.benchmark, snap.as_of, cfg.risk.trend_days)
            if cfg.sector_relative.enabled and (lines := relative_lines(snap, tickers)):
                sections.append(
                    "## Against the sector (return minus the sector fund's)\n" + "\n".join(lines)
                )
            if cfg.insiders.enabled:
                self.clusters = signals.insider_clusters(session, snap.as_of, cfg.insiders, tickers)
                if self.clusters:
                    lines = [
                        f"- {t}: {c.summary(cfg.insiders.window_days)}"
                        for t, c in sorted(self.clusters.items())
                    ]
                    sections.append("## Insider cluster buying (Form 4)\n" + "\n".join(lines))
                    for t, c in self.clusters.items():
                        self.context.setdefault(t, []).append(
                            "insiders: " + c.summary(cfg.insiders.window_days)
                        )
            kinds = [
                k
                for k, on in (("options", cfg.options.enabled), ("analysts", cfg.analysts.enabled))
                if on
            ]
            if kinds:
                found = signals.stored(session, new_york_date(snap.as_of), kinds)
                lines = [
                    f"- {t} ({row.kind}): {row.summary}"
                    for t in tickers
                    for row in found.get(t, [])
                ]
                if lines:
                    sections.append("## Options and analyst signals\n" + "\n".join(lines))
                for t in tickers:
                    for row in found.get(t, []):
                        self.context.setdefault(t, []).append(f"{row.kind}: {row.summary}")
                if not found:
                    notes.append("No options or analyst signals today (see the 08:00 job).")
            if cfg.confidence.calibrate or cfg.confidence.abstain or cfg.meta_label.enabled:
                self._learn(session, notes)
            session.commit()
        if self.market is not None and cfg.risk.spy_trend_filter != "off":
            notes.append(
                f"{snap.benchmark} is {self.market.label} its {self.market.days}-day average."
            )
        extra = ""
        if sections:
            extra = ("# Extra information from system B's rules (facts, not recommendations)\n\n"
                     + "\n\n".join(sections))  # fmt: skip
        return kept, weights, extra, notes

    def _learn(self, session: Session, notes: list[str]) -> None:
        conf = self.cfg.confidence
        samples = all_samples(self.engine, session, self.config, self.now, conf.window_days)
        resolved = resolve(session, self.config, samples, self.now,
                           sector_relative=self.cfg.sector_relative.enabled)  # fmt: skip
        if conf.calibrate or conf.abstain:
            self.calibrator, row = calibrate.fit(resolved, conf, self.now)
            session.add(row)
            notes.append(f"Calibration: {self.calibrator.note}.")
        if self.cfg.meta_label.enabled:
            self.meta_model = meta.train(resolved, self.cfg.meta_label)
            notes.append(f"Meta-labelling: {self.meta_model.note}.")

    # ------------------------------------------------------------------ after the judge

    async def apply(
        self,
        decision: JudgeDecision,
        *,
        reports: Sequence[AgentReport],
        snap: Snapshot,
        claude: ClaudeClient,
        cycle_id: int,
    ) -> JudgeDecision:
        cfg = self.cfg
        trades: list[RankedTrade] = []
        for trade in decision.trades:
            trades.append(self._score(trade, snap, decision.threshold))

        if cfg.postmortems.enabled and cfg.postmortems.apply_to_judge:
            trades = await self._lessons(
                trades, decision.threshold, reports, snap, claude, cycle_id
            )

        if cfg.veto.enabled:
            live = [t for t in trades if t.status == "proposed"]
            verdicts = await veto.review(
                live, reports, snap, claude, cfg.veto, cycle_id, self.context
            )
            for index, t in enumerate(trades):
                if t.ticker not in verdicts:
                    continue
                v = verdicts[t.ticker]
                entry = self._logs[t.ticker]
                entry["veto"], entry["veto_reason"] = v.verdict, v.reason
                if v.verdict == "veto":
                    trades[index] = t.model_copy(update={"status": "rejected_by_risk",
                                                         "status_reason": f"vetoed by the review: {v.reason}"})  # fmt: skip
                    entry["steps"].append(f"3 review: vetoed ({v.reason})")
                elif v.verdict == "shrink":
                    size = round(t.size_pct * cfg.veto.shrink_to, 4)
                    trades[index] = t.model_copy(update={"size_pct": size})
                    entry["steps"].append(f"3 review: size cut to {size:.1%} ({v.reason})")
                else:
                    entry["steps"].append(f"3 review: {v.verdict}")

        # Rank again on B's own numbers, and keep today's maximum. A past case's nudge
        # upwards never decides who makes the cut: ranking uses the lower of the two.
        trades.sort(key=lambda t: (t.status != "proposed", -min(t.final_confidence,
                                                                self._before_lessons.get(t.ticker, 1.0))))  # fmt: skip
        limit = self.config.judge.max_trades_per_day
        ranked = []
        for index, t in enumerate(trades, start=1):
            update: dict[str, Any] = {"rank": index}
            if t.status == "proposed" and index > limit:
                update |= {"status": "below_threshold",
                           "status_reason": f"ranked below today's maximum of {limit} trades"}  # fmt: skip
            ranked.append(t.model_copy(update=update))
        for t in ranked:
            entry = self._logs[t.ticker]
            entry.update(b_status=t.status, b_reason=t.status_reason, b_size=t.size_pct,
                         b_stop=t.stop_price, b_target=t.target_price)  # fmt: skip
        bar = cfg.confidence.calibrated_threshold if self.calibrator.active else decision.threshold
        return decision.model_copy(update={"trades": ranked, "threshold": bar})

    def _score(self, t: RankedTrade, snap: Snapshot, threshold: float) -> RankedTrade:
        cfg = self.cfg
        steps: list[str] = []
        entry: dict[str, Any] = {
            "ticker": t.ticker, "action": t.action, "horizon_days": t.horizon_days,
            "judge_confidence": t.judge_confidence, "agreement": t.agent_agreement,
            "a_status": t.status, "a_reason": t.status_reason, "a_size": t.size_pct,
            "a_stop": t.stop_price, "a_target": t.target_price, "conformal": "off", "veto": "off",
            "market_trend": self.market.label if self.market else None,
            "switches": cfg.switches(), "steps": steps, "signals": {},
        }  # fmt: skip
        self._logs[t.ticker] = entry
        if t.ticker in self.clusters:
            c = self.clusters[t.ticker]
            entry["signals"]["insider_cluster"] = {
                "buyers": len(c.buyers),
                "bought_usd": round(c.bought_usd),
            }
        ind = snap.indicators.get(t.ticker)
        entry["atr_pct"] = ind.atr_pct if ind else None
        raw = t.final_confidence
        if not cfg.confidence.use_pattern_model:
            raw = raw_blend(t.judge_confidence, t.agent_agreement, self.config.judge.blend)
            if t.model_probability is not None:
                steps.append(f"1 pattern model left out: {t.final_confidence:.2f} -> {raw:.2f}")
        entry["raw_confidence"] = raw
        if t.status == "rejected_by_risk":  # the judge's own checks refused it: B keeps that
            entry["calibrated"] = None
            return t

        p, bar = raw, threshold
        if (
            cfg.insiders.enabled
            and cfg.insiders.confidence_bonus
            and t.ticker in self.clusters
            and t.action == "buy"
        ):
            p = min(p + cfg.insiders.confidence_bonus, 1.0)
            steps.append(f"5 insider cluster: +{cfg.insiders.confidence_bonus:.2f}")
        if cfg.confidence.calibrate:
            if self.calibrator.active:
                calibrated = self.calibrator.probability(p)
                steps.append(
                    f"1 calibrated: {p:.2f} -> {calibrated:.2f} (bar {cfg.confidence.calibrated_threshold:.2f})"
                )
                p, bar = calibrated, cfg.confidence.calibrated_threshold
            else:
                steps.append(f"1 calibration {self.calibrator.note}; A's bar kept")
        entry["calibrated"] = p if self.calibrator.active else None
        status, reason = ("proposed", None) if p >= bar else (
            "below_threshold", f"confidence {p:.2f} is below {bar:.2f}")  # fmt: skip

        if cfg.confidence.abstain:
            verdict = self.calibrator.verdict(p)
            entry["conformal"] = verdict
            if verdict == "abstain" and status == "proposed":
                q = self.calibrator.quantile or 1.0
                status, reason = "below_threshold", (
                    f"abstained: at {p:.2f} the past data cannot tell success from failure "
                    f"(needs above {max(q, 1 - q):.2f})")  # fmt: skip
                steps.append("1 abstained")

        if cfg.meta_label.enabled:
            sample = IdeaSample(snap.as_of, t.ticker, t.action, t.horizon_days, raw, "B",
                                {"judge": t.judge_confidence, "agreement": t.agent_agreement})  # fmt: skip
            meta_p = self.meta_model.probability(sample)
            entry["meta_probability"] = meta_p
            if meta_p is not None and meta_p < cfg.meta_label.threshold and status == "proposed":
                status, reason = (
                    "below_threshold",
                    f"meta-label gives it {meta_p:.2f}, below {cfg.meta_label.threshold:.2f}",
                )
                steps.append(f"10 meta-label: {meta_p:.2f}")

        stop, target, size = t.stop_price, t.target_price, t.size_pct
        reference = (t.entry_low + t.entry_high) / 2
        entry["entry_reference"] = reference
        buy = t.action == "buy"
        daily_range = ind.atr_pct * ind.last_close if ind and ind.atr_pct else None
        risk = cfg.risk
        if risk.atr_brackets and daily_range:
            if buy:
                stop = min(
                    reference - risk.atr_stop_multiple * daily_range, t.entry_low * (1 - MIN_GAP)
                )
                target = max(
                    reference + risk.atr_target_multiple * daily_range, t.entry_high * (1 + MIN_GAP)
                )
            else:
                stop = max(
                    reference + risk.atr_stop_multiple * daily_range, t.entry_high * (1 + MIN_GAP)
                )
                target = min(
                    reference - risk.atr_target_multiple * daily_range, t.entry_low * (1 - MIN_GAP)
                )
            stop, target = _cents(max(stop, 0.01)), _cents(max(target, 0.01))
            steps.append(
                f"2 ATR brackets: stop {t.stop_price:.2f} -> {stop:.2f}, target {t.target_price:.2f} -> {target:.2f}"
            )
        elif risk.atr_brackets:
            steps.append("2 ATR brackets: no daily range known; the judge's levels kept")
        if risk.vol_sizing and abs(reference - stop) > 0:
            new = min(
                risk.risk_per_trade_pct * reference / abs(reference - stop),
                self.config.risk.max_position_pct,
            )
            steps.append(f"2 sized by risk at the stop: {size:.1%} -> {new:.1%}")
            size = round(new, 4)
        if (
            risk.spy_trend_filter != "off"
            and buy
            and self.market is not None
            and not self.market.above
        ):
            if risk.spy_trend_filter == "block" and status == "proposed":
                status, reason = (
                    "below_threshold",
                    f"{snap.benchmark} is below its {self.market.days}-day average",
                )
                steps.append("2 market filter: blocked")
            elif risk.spy_trend_filter == "halve":
                size = round(size / 2, 4)
                steps.append(f"2 market filter: size halved to {size:.1%}")

        if cfg.cost_band.enabled and status == "proposed" and reference > 0:
            gain, loss = abs(target - reference) / reference, abs(reference - stop) / reference
            edge = p * gain - (1 - p) * loss
            costs = cfg.cost_band.round_trip_bps / 10_000
            if edge < cfg.cost_band.margin * costs:
                status, reason = "below_threshold", (
                    f"expected edge {edge:+.2%} does not clear {cfg.cost_band.margin:g}x the costs ({costs:.2%})")  # fmt: skip
                steps.append(f"7 cost band: edge {edge:+.2%}")

        return t.model_copy(update={
            "final_confidence": round(p, 4),
            "model_probability": t.model_probability if cfg.confidence.use_pattern_model else None,
            "stop_price": stop, "target_price": target, "size_pct": size,
            "status": status, "status_reason": reason,
        })  # fmt: skip

    async def _lessons(self, trades: list[RankedTrade], threshold: float, reports: Sequence[AgentReport],
                       snap: Snapshot, claude: ClaudeClient, cycle_id: int) -> list[RankedTrade]:  # fmt: skip
        """X2c: similar past cases, judged for comparability, may nudge a trade's confidence."""
        pm = self.cfg.postmortems
        bar = self.cfg.confidence.calibrated_threshold if self.calibrator.active else threshold
        by_id = {r.report_id: r for r in reports}
        items: list[tuple[RankedTrade, postmortem.Shown]] = []
        with self.sessions() as session:
            for t in trades:
                if t.status != "proposed":
                    continue  # a past case can only make B more careful about a trade it would place
                member = snap.members.get(t.ticker)
                ind = snap.indicators.get(t.ticker)
                conditions = {
                    "event_types": sorted({e for i in t.supporting if (r := by_id.get(i)) and (e := r.event_type)}),
                    "sector": member.sector if member else None,
                    "market_trend": self.market.label if self.market else None,
                    "vol": postmortem.vol_bucket(ind.atr_pct if ind else None),
                    "direction": "long" if t.action == "buy" else "short",
                }  # fmt: skip
                found = postmortem.similar(session, pm, t.ticker, conditions, snap.as_of)
                if found:
                    items.append((t, postmortem.Shown(t.ticker, conditions, found)))
        if not items:
            return trades
        try:
            answer = await claude.structured(
                purpose="lesson_compare", model=pm.summary_model, effort="medium", max_tokens=8000,
                system=postmortem.COMPARE, user=postmortem.render_compare(items),
                output=postmortem.CompareOutput, cycle_id=cycle_id,
            )  # fmt: skip
            verdicts = {(v.ticker.strip().upper(), v.case_id): v for v in answer.output.verdicts}
        except (LLMError, BudgetExceededError) as exc:
            log.warning("lessons not applied", extra={"error": str(exc)})
            verdicts = {}  # fails open: no change
        changed: dict[str, RankedTrade] = {}
        for t, shown in items:
            total, comparable, why = 0.0, [], []
            for case, _, _ in shown.cases:
                self._shown.setdefault(case.id, False)
                v = verdicts.get((t.ticker, case.id))
                if v is None or not v.comparable:
                    continue
                self._shown[case.id] = True
                comparable.append(case.id)
                total += postmortem.adjustment(pm, case, v.change)
                why.append(f"case {case.id}: {clip(v.why, 120)}")
            total = round(max(-pm.cap_supported, min(pm.cap_supported, total)), 4)
            before = t.final_confidence
            after = round(min(max(before + total, 0.0), 1.0), 4)
            update: dict[str, Any] = {"final_confidence": after}
            status = t.status
            if total < 0 and after < bar:
                status = "below_threshold"
                update |= {"status": status, "status_reason":
                           f"past cases lowered it to {after:.2f}, below {bar:.2f} ({'; '.join(why)})"}  # fmt: skip
            self._before_lessons[t.ticker] = before
            entry = self._logs[t.ticker]
            entry["steps"].append(
                f"X2c lessons: {total:+.2f} from cases {comparable or 'none comparable'}"
            )
            self._lesson_uses.append(LessonUse(
                as_of=snap.as_of, ticker=t.ticker, action=t.action, horizon_days=t.horizon_days,
                case_ids=[c.id for c, _, _ in shown.cases], comparable_ids=comparable, adjustment=total,
                confidence_without=before, confidence_with=after, status_without=t.status, status_with=status,
                reasons="; ".join(why),
            ))  # fmt: skip
            if total:
                changed[t.ticker] = t.model_copy(update=update)
        return [changed.get(t.ticker, t) for t in trades]

    # ------------------------------------------------------------------ the record

    def record(self, session: Session, row: m.JudgeDecision) -> None:
        for idea in row.ideas:
            entry = self._logs.get(idea.ticker)
            if entry is None:
                continue
            values = {k: v for k, v in entry.items() if k in DecisionLog.__table__.columns}
            values.setdefault("b_status", entry["a_status"])
            values.setdefault("b_reason", entry["a_reason"])
            values.setdefault("b_size", entry["a_size"])
            values.setdefault("b_stop", entry["a_stop"])
            values.setdefault("b_target", entry["a_target"])
            session.add(DecisionLog(decision_id=row.id, idea_id=idea.id, cycle_id=row.cycle_id,
                                    as_of=row.as_of, **values))  # fmt: skip
        for use in self._lesson_uses:
            session.add(use)
        for case_id, comparable in self._shown.items():
            case = session.get(PostMortem, case_id)
            if case is not None:
                case.times_shown += 1
                case.times_comparable += 1 if comparable else 0
        if self.pruned:
            log.info("agents left out", extra={"agents": self.pruned})
