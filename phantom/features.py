"""State engine: packs closed candles into the one compact numeric snapshot Jev sees.

Only candles that closed at or before the decision time are used. The snapshot holds no
dates, ticker or raw prices, only returns and ATR- or vol-normalized values.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass

SCHEMA_VERSION = "snap-v1"
LONG_WINDOW = 96  # 24 hours of 15m candles

SNAPSHOT_FIELDS = (
    "ret_1", "ret_4", "ret_16",
    "dist_high_atr", "dist_low_atr", "range_width_atr",
    "rv_short", "rv_long", "rv_ratio",
    "ema_slope_atr", "trend_efficiency",
    "close_location", "volume_z",
    "in_position", "bars_in_trade", "open_r",
)


@dataclass(frozen=True)
class Candle:
    start: int  # unix seconds when the candle opened
    open: float
    high: float
    low: float
    close: float
    volume: float
    seconds: int = 900

    @property
    def end(self) -> int:
        return self.start + self.seconds


def closed_before(candles: list[Candle], decision_time: int) -> list[Candle]:
    return [c for c in candles if c.end <= decision_time]


def atr(candles: list[Candle], n: int) -> float:
    trs = []
    for prev, c in zip(candles[-n - 1:-1], candles[-n:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return sum(trs) / len(trs)


def _ema(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def build_snapshot(candles: list[Candle], decision_time: int, lookback: int = 32, atr_n: int = 14,
                   position: dict | None = None) -> dict[str, float] | None:
    cs = closed_before(candles, decision_time)
    if len(cs) < LONG_WINDOW + 2 or len(cs) < lookback + atr_n + 2:
        return None
    closes = [c.close for c in cs]
    last = cs[-1]
    a = atr(cs, atr_n) or 1e-9
    prior = cs[-lookback - 1:-1]
    rh, rl = max(c.high for c in prior), min(c.low for c in prior)
    logret = [math.log(b / a_) for a_, b in zip(closes[-LONG_WINDOW - 1:-1], closes[-LONG_WINDOW:])]
    rv_s = statistics.pstdev(logret[-16:]) or 1e-9
    rv_l = statistics.pstdev(logret) or 1e-9
    ema = _ema(closes[-LONG_WINDOW:], 20)
    path = sum(abs(r) for r in logret[-32:]) or 1e-9
    vols = [c.volume for c in cs[-LONG_WINDOW:]]
    v_sd = statistics.pstdev(vols) or 1e-9
    rng = (last.high - last.low) or 1e-9
    pos = position or {}
    snap = {
        "ret_1": math.log(closes[-1] / closes[-2]) * 100,
        "ret_4": math.log(closes[-1] / closes[-5]) * 100,
        "ret_16": math.log(closes[-1] / closes[-17]) * 100,
        "dist_high_atr": (last.close - rh) / a,
        "dist_low_atr": (last.close - rl) / a,
        "range_width_atr": (rh - rl) / a,
        "rv_short": rv_s * 100,
        "rv_long": rv_l * 100,
        "rv_ratio": rv_s / rv_l,
        "ema_slope_atr": (ema[-1] - ema[-9]) / a,
        "trend_efficiency": sum(logret[-32:]) / path,
        "close_location": (last.close - last.low) / rng,
        "volume_z": (last.volume - statistics.fmean(vols)) / v_sd,
        "in_position": 1 if pos else 0,
        "bars_in_trade": int(pos.get("bars", 0)),
        "open_r": float(pos.get("open_r", 0.0)),
    }
    return {k: round(float(v), 4) for k, v in snap.items()}


def snapshot_hash(snap: dict[str, float]) -> str:
    blob = json.dumps({"schema": SCHEMA_VERSION, **dict(sorted(snap.items()))}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
