"""Jev: the fast decision layer. One call per candle, six fixed-outcome questions, numbers back.

Jev sees only the numeric snapshot plus a legend of what each field means. Answers are cached
by snapshot hash so backtests are reproducible and repeat calls are free.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from phantom.config import StrategyConfig
from phantom.features import SCHEMA_VERSION, snapshot_hash
from phantom.strategy import JevView

LEGEND = {
    "ret_1": "log return of the last candle, percent",
    "ret_4": "log return over the last 4 candles (1 hour), percent",
    "ret_16": "log return over the last 16 candles (4 hours), percent",
    "dist_high_atr": "close minus the highest high of the prior 32 candles, in ATRs (positive = breaking out above the range)",
    "dist_low_atr": "close minus the lowest low of the prior 32 candles, in ATRs",
    "range_width_atr": "height of the prior 32-candle range, in ATRs",
    "rv_short": "realized volatility of the last 16 candle returns, percent",
    "rv_long": "realized volatility of the last 96 candle returns, percent",
    "rv_ratio": "rv_short divided by rv_long (above 1 = volatility expanding)",
    "ema_slope_atr": "change of the 20-candle EMA over 8 candles, in ATRs",
    "trend_efficiency": "net move over 32 candles divided by the total path length, -1 to 1",
    "close_location": "where the last close sits in its candle's range, 0 = low, 1 = high",
    "volume_z": "last candle's volume as a z-score vs the last 96 candles",
    "in_position": "1 if already long, else 0",
    "bars_in_trade": "candles since entry, if long",
    "open_r": "open profit in multiples of the initial risk, if long",
}


def questions(cfg: StrategyConfig) -> dict:
    return {
        "regime": {"type": "choice", "instructions": "Which market regime is this snapshot in?",
                   "criteria": {"trending_up": "Sustained upward trend", "trending_down": "Sustained downward trend",
                                "ranging": "Sideways within a range", "choppy": "Erratic, no clean direction"}},
        "direction": {"type": "choice", "instructions": f"Over the next {cfg.time_stop_candles} candles, which way does price move?",
                      "criteria": {"up": None, "down": None, "sideways": "Moves less than 1 ATR either way"}},
        "buying_pressure_real": {"type": "noul", "instructions": "The current buying pressure is real and continues for the next few candles rather than fading."},
        "setup_quality": {"type": "score", "instructions": "Rate the quality of this upside breakout setup.",
                          "criteria": ["No setup", "Weak: likely to fail", "Average", "Good: clean break with support", "Excellent: strong, clean, well-supported break"]},
        "risk_state": {"type": "choice", "instructions": "How risky is the market state right now?",
                       "criteria": {"normal": None, "elevated": "Volatility or instability above usual",
                                    "extreme": "Disorderly, crash-like or squeeze-like conditions"}},
        "tp_before_sl": {"type": "noul", "instructions":
                         f"Starting from this close, price rises {cfg.take_profit_atr} ATR before it falls {cfg.stop_atr} ATR, "
                         f"within the next {cfg.time_stop_candles} candles."},
    }


def build_state(snap: dict) -> dict:
    return {"description": "Snapshot of a liquid crypto market against USD at a 15-minute candle close. "
                           "All values are normalized; there are no prices, dates or names.",
            "fields": LEGEND, "snapshot": snap}


def parse_answers(answers: dict) -> JevView:
    """`answers` is {name: answer-dict} as returned by the API (or an SDK response's model_dump)."""
    regime, risk = answers["regime"], answers["risk_state"]
    return JevView(
        regime_up=float(regime["probabilities"]["trending_up"]), regime_top=str(regime["choice"]),
        dir_up=float(answers["direction"]["probabilities"]["up"]),
        pressure=float(answers["buying_pressure_real"]["noul"]),
        quality=float(answers["setup_quality"]["score"]),
        extreme=float(risk["probabilities"]["extreme"]), risk_top=str(risk["choice"]),
        tp=float(answers["tp_before_sl"]["noul"]),
    )


class TypeSafeJev:
    """Calls Jev through TypeSafe's SDK (reads TYPESAFE_API_KEY from the environment)."""

    def __init__(self, cfg: StrategyConfig, cache_path: str | Path = "state/jev_cache.sqlite", model: str | None = None,
                 timeout: float = 5.0, recalibrate_tp: Callable[[float], float] | None = None):
        from typesafe_sdk import RetryPolicy, TypeSafeClient  # imported here so tests don't need the SDK
        self.client = TypeSafeClient(model=model, timeout=timeout, retry=RetryPolicy(max_retries=1))
        self.model = model or "default"
        self.questions = questions(cfg)
        self.recalibrate_tp = recalibrate_tp
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, answers TEXT)")

    def _key(self, snap: dict) -> str:
        return f"{self.model}|{SCHEMA_VERSION}|{snapshot_hash(snap)}"

    def ask(self, snap: dict, use_cache: bool = True) -> tuple[JevView, dict, float]:
        key = self._key(snap)
        row = self.db.execute("SELECT answers FROM cache WHERE k=?", (key,)).fetchone() if use_cache else None
        t0 = time.perf_counter()
        if row:
            answers, latency = json.loads(row[0]), 0.0
        else:
            resp = self.client.system_one(state=build_state(snap), questions=self.questions)
            latency = (time.perf_counter() - t0) * 1000
            answers = {k: v.model_dump() for k, v in resp.answers.items()}
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?, ?)", (key, json.dumps(answers)))
            self.db.commit()
        view = parse_answers(answers)
        raw_tp = view.tp
        if self.recalibrate_tp:
            view = view._replace(tp=self.recalibrate_tp(view.tp))
        return view, {"answers": answers, "raw_tp": raw_tp}, latency
