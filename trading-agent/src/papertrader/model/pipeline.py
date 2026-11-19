"""Build the dataset, validate walk-forward, backtest, train the final model, report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.snapshot import SECTOR_ETF
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.model.backtest import run_backtest
from papertrader.model.dataset import build_dataset, load_bars, save_dataset
from papertrader.model.registry import Promotion, decide_promotion, save_version
from papertrader.model.report import to_html, to_markdown
from papertrader.model.train import (
    MIN_TRAIN_ROWS,
    calibration_table,
    daily_ic,
    fit,
    importance,
    metrics,
    walk_forward,
)

KEEP_DATASETS = 3  # older training datasets are deleted: each can be rebuilt from the database


def prune_datasets(folder: Path, keep: int = KEEP_DATASETS) -> list[Path]:
    """Delete all but the newest `keep` dataset files, so weekly retraining cannot fill the disk."""
    old = sorted(folder.glob("dataset_*.parquet"), reverse=True)[keep:]
    for path in old:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
    return old


class NotEnoughDataError(RuntimeError):
    pass


@dataclass
class TrainingResult:
    version: str
    promoted: bool
    report: dict[str, Any]
    markdown_path: Path
    html_path: Path
    dataset_path: Path


def _data_summary(table: pd.DataFrame, horizon: int) -> dict[str, Any]:
    labelled = table.dropna(subset=[f"up_{horizon}d"])
    llm = int((table["llm_count"] > 0).sum())
    return {
        "rows": len(labelled), "tickers": int(table["ticker"].nunique()),
        "first_day": str(table["day"].min()), "last_day": str(table["day"].max()),
        "base_rate": float(labelled[f"up_{horizon}d"].mean()) if len(labelled) else None,
        "llm_rows": llm, "llm_share": llm / len(table) if len(table) else 0.0,
        "events": {
            "news days": int((table["news_count"] > 0).sum()),
            "earnings releases": int((table["has_earnings"] > 0).sum()),
            "8-K filings": int((table["has_8k"] > 0).sum()),
            "insider purchases": int((table["insider_buyers"] > 0).sum()),
        },
    }  # fmt: skip


def run_training(
    sessions: sessionmaker[Session], config: AppConfig, *, as_of: datetime, data_dir: Path
) -> TrainingResult:
    cfg = config.pattern_model
    horizon = cfg.primary_horizon_days
    folder = data_dir / "model"

    with sessions() as session:
        table = build_dataset(session, config, as_of=as_of)
        tickers = (
            [*table["ticker"].unique(), config.universe.benchmark, *SECTOR_ETF.values()]
            if len(table)
            else []
        )
        bars = load_bars(session, list(tickers), None, as_of) if len(table) else pd.DataFrame()
    labelled = table.dropna(subset=[f"up_{horizon}d"]) if len(table) else table
    if len(labelled) < MIN_TRAIN_ROWS * 2:
        raise NotEnoughDataError(
            f"only {len(labelled)} usable rows; at least {MIN_TRAIN_ROWS * 2} are needed. "
            "Load more history with `papertrader ingest backfill` first."
        )
    dataset_path = save_dataset(table, folder, as_of=as_of)
    prune_datasets(folder, keep=KEEP_DATASETS)

    validation = walk_forward(table, cfg, horizon)
    scored = validation.predictions
    if scored.empty:
        raise NotEnoughDataError(
            "no walk-forward fold had enough training data: " + "; ".join(validation.skipped)
        )
    backtest = run_backtest(
        scored, bars, benchmark=config.universe.benchmark, cfg=cfg, risk=config.risk,
        horizon=horizon, threshold=cfg.backtest_threshold, top_n=cfg.backtest_top_n,
    )  # fmt: skip

    last_fold = scored[scored["fold"] == scored["fold"].max()]
    candidate_ic = daily_ic(last_fold, horizon)
    final = fit(table, cfg, horizon)  # the model that would go live: trained on everything

    report: dict[str, Any] = {
        "generated_at": f"{as_of:%Y-%m-%d %H:%M} UTC",
        "horizon": horizon, "embargo_days": cfg.embargo_days,
        "position_pct": config.risk.max_position_pct, "max_positions": config.risk.max_open_positions,
        "data": _data_summary(table, horizon),
        "folds": validation.folds, "skipped": validation.skipped,
        "oos": metrics(scored, horizon), "calibration": calibration_table(scored, horizon),
        "backtest": backtest, "importance": importance(final),
        "calibration_method": final.calibration,
    }  # fmt: skip

    with sessions() as session:
        promotion: Promotion = decide_promotion(
            session, last_fold, candidate_ic, horizon,
            brier=report["oos"].get("brier"), brier_naive=report["oos"].get("brier_naive"),
        )  # fmt: skip
        report["promotion"] = (
            ("Promoted to active: " if promotion.promoted else "NOT promoted: ") + promotion.reason
            + f" (new model IC {promotion.candidate_ic if promotion.candidate_ic is None else round(promotion.candidate_ic, 4)}"
            + f", active model {promotion.current_ic if promotion.current_ic is None else round(float(promotion.current_ic), 4)})."
        )  # fmt: skip
        row: m.ModelVersion = save_version(
            session, final, folder=folder, trained_from=table["day"].min(),
            trained_to=labelled["day"].max(),
            metrics={**report["oos"], "latest_window_ic": candidate_ic,
                     "backtest": {k: backtest.get(k) for k in ("strategy", "benchmark", "random")}},
            params={"horizon": horizon, "calibration": cfg.calibration, "embargo_days": cfg.embargo_days,
                    "splits": cfg.walk_forward_splits, "dataset": dataset_path.name},
            promotion=promotion, now=as_of,
        )  # fmt: skip
        report["version"] = row.version
        session.commit()
        version, promoted = row.version, row.is_active

    markdown = to_markdown(report)
    markdown_path = folder / version / "backtest_report.md"
    html_path = folder / version / "backtest_report.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(to_html(markdown, f"Pattern model backtest {version}"), encoding="utf-8")
    (folder / version / "report.json").write_text(json.dumps(report, indent=2, default=str))
    return TrainingResult(version, promoted, report, markdown_path, html_path, dataset_path)
