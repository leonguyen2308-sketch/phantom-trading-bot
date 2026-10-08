"""Deterministic strategy rules. Jev's answers are inputs; this code decides."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from phantom.config import StrategyConfig


class JevView(NamedTuple):
    """Jev's answers reduced to the numbers the rules use (after recalibration, if any)."""
    regime_up: float
    regime_top: str
    dir_up: float
    pressure: float
    quality: float  # expected score, 0..4
    extreme: float
    risk_top: str
    tp: float  # P(take-profit before stop)


@dataclass
class Evaluation:
    fire: bool
    score: float
    failures: list[str] = field(default_factory=list)


def is_breakout(snap: dict[str, float], cfg: StrategyConfig) -> bool:
    return (snap["dist_high_atr"] > cfg.min_breakout_atr
            and snap["range_width_atr"] >= cfg.min_range_atr
            and snap["rv_ratio"] <= cfg.max_rv_ratio)


def combined_score(v: JevView, cfg: StrategyConfig) -> float:
    w = cfg.weights
    return (w["regime"] * v.regime_up + w["direction"] * v.dir_up + w["pressure"] * v.pressure
            + w["quality"] * (v.quality / 4) + w["tp"] * v.tp)


def evaluate(v: JevView, cfg: StrategyConfig) -> Evaluation:
    t = cfg.thresholds
    sc = combined_score(v, cfg)
    checks = [
        (v.regime_up >= t["regime_up_min"], "regime not trending up"),
        (v.dir_up >= t["dir_up_min"], "direction not up"),
        (v.pressure >= t["pressure_min"], "buying pressure not convincing"),
        (v.quality >= t["quality_min"], "setup quality too low"),
        (v.extreme <= t["extreme_max"], "risk state too high"),
        (v.tp >= t["tp_min"], "TP-before-stop probability too low"),
        (sc >= t["score_min"], "weighted score below threshold"),
    ]
    failures = [msg for ok, msg in checks if not ok]
    return Evaluation(fire=not failures, score=sc, failures=failures)


def exit_levels(entry: float, atr_value: float, cfg: StrategyConfig) -> tuple[float, float]:
    return entry - cfg.stop_atr * atr_value, entry + cfg.take_profit_atr * atr_value
