"""Backtest harness. The harness runs the numbers and grades them; no model grades its own work.

Decisions happen at a candle's close using only closed candles; entries fill at the next
candle's open. Fees and slippage apply on both sides. If a candle touches both the stop and
the take-profit, it counts as the stop.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Callable

from phantom.config import RiskLimits, StrategyConfig
from phantom.features import Candle, atr, build_snapshot
from phantom.sizing import KellyConfig, position_size
from phantom.strategy import JevView, evaluate, exit_levels, is_breakout

JevProvider = Callable[[dict], JevView]
WINDOW = 200  # candles handed to the snapshot builder


@dataclass(frozen=True)
class Costs:
    fee_bps: float
    slippage_bps: float

    def doubled(self) -> "Costs":
        return Costs(self.fee_bps * 2, self.slippage_bps * 2)


@dataclass
class Trade:
    signal_time: int
    entry_time: int
    entry_price: float
    stop: float
    tp: float
    qty: float
    prob: float | None
    regime: str
    exit_time: int = 0
    exit_price: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    r: float = 0.0


@dataclass
class Result:
    trades: list[Trade]
    equity: list[tuple[int, float]]
    killed: str | None = None
    candidates: int = 0
    jev_calls: int = 0
    tp_labels: list[tuple[float, int]] = field(default_factory=list)


def regime_label(snap: dict) -> str:
    te, vr = snap["trend_efficiency"], snap["rv_ratio"]
    trend = "up" if te > 0.25 else "down" if te < -0.25 else "range"
    return f"{trend}/{'high' if vr > 1.3 else 'low'}-vol"


def _exit_in_candle(c: Candle, stop: float, tp: float) -> tuple[str, float] | None:
    if c.open <= stop:
        return "stop", c.open  # gapped through the stop
    if c.open >= tp:
        return "tp", c.open
    if c.low <= stop:
        return "stop", stop  # checked first: conservative when both are touched
    if c.high >= tp:
        return "tp", tp
    return None


def run_backtest(candles: list[Candle], cfg: StrategyConfig, limits: RiskLimits, jev: JevProvider | None,
                 costs: Costs, start_equity: float, kelly_enabled: bool = False) -> Result:
    fee, slip = costs.fee_bps / 1e4, costs.slippage_bps / 1e4
    kelly = KellyConfig(**cfg.kelly)
    cash, pos = start_equity, None
    peak = day_start = start_equity
    day = None
    res = Result(trades=[], equity=[])
    for i in range(len(candles) - 1):
        c = candles[i]
        if pos and c.start >= pos.entry_time:
            hit = _exit_in_candle(c, pos.stop, pos.tp)
            bars = (c.end - pos.entry_time) // c.seconds
            if not hit and bars >= cfg.time_stop_candles:
                hit = ("time", c.close)
            if hit:
                px = hit[1] * (1 - slip)
                cash += pos.qty * px - pos.qty * px * fee
                pos.exit_time, pos.exit_price, pos.reason = c.end, px, hit[0]
                pos.pnl = pos.qty * (px - pos.entry_price) - pos.qty * (px + pos.entry_price) * fee
                pos.r = pos.pnl / (pos.qty * (pos.entry_price - pos.stop))
                res.trades.append(pos)
                pos = None
        eq = cash + (pos.qty * c.close if pos else 0.0)
        res.equity.append((c.end, eq))
        d = datetime.fromtimestamp(c.end, timezone.utc).date()
        if d != day:
            day, day_start = d, eq
        peak = max(peak, eq)
        if not res.killed and (peak - eq) / peak >= limits.max_drawdown_pct:
            res.killed = f"max drawdown hit at {datetime.fromtimestamp(c.end, timezone.utc):%Y-%m-%d %H:%M}"
        if res.killed or pos:
            continue
        if (day_start - eq) / day_start >= limits.daily_loss_limit_pct:
            continue
        window = candles[max(0, i - WINDOW):i + 1]
        snap = build_snapshot(window, c.end, cfg.breakout_lookback, cfg.atr_period)
        if not snap or not is_breakout(snap, cfg):
            continue
        res.candidates += 1
        p = None
        if jev is not None:
            view = jev(snap)
            res.jev_calls += 1
            p = view.tp
            if not evaluate(view, cfg).fire:
                continue
        nxt = candles[i + 1]
        entry = nxt.open * (1 + slip)
        stop, tp = exit_levels(entry, atr(window, cfg.atr_period), cfg)
        size = position_size(equity=eq, entry=entry, stop=stop, p=p if p is not None else 0.0, b=cfg.payoff_ratio,
                             kelly=kelly, limits=limits, kelly_enabled=kelly_enabled and p is not None,
                             fixed_risk_pct=cfg.fixed_risk_pct)
        if size.qty <= 0:
            continue
        cash -= size.qty * entry + size.qty * entry * fee
        pos = Trade(signal_time=c.end, entry_time=nxt.start, entry_price=entry, stop=stop, tp=tp,
                    qty=size.qty, prob=p, regime=regime_label(snap))
    return res


# ---------- metrics ----------

def daily_returns(equity: list[tuple[int, float]]) -> list[float]:
    closes: dict = {}
    for t, v in equity:
        closes[datetime.fromtimestamp(t, timezone.utc).date()] = v
    vals = list(closes.values())
    return [b / a - 1 for a, b in zip(vals, vals[1:]) if a > 0]


def newey_west_t(xs: list[float], lags: int = 5) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    d = [x - m for x in xs]
    var = sum(v * v for v in d) / n
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1 - lag / (lags + 1)
        var += 2 * w * sum(d[t] * d[t - lag] for t in range(lag, n)) / n
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def max_drawdown(equity: list[tuple[int, float]]) -> float:
    peak, worst = -math.inf, 0.0
    for _, v in equity:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak if peak > 0 else 0)
    return worst


def metrics(equity: list[tuple[int, float]], trades: list[Trade]) -> dict:
    rets = daily_returns(equity)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    mean = statistics.fmean(rets) if rets else 0.0
    sharpe = mean / sd * math.sqrt(365) if sd > 0 else 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    return {
        "days": len(rets),
        "total_return": equity[-1][1] / equity[0][1] - 1 if equity else 0.0,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown(equity),
        "hit_rate": wins / len(trades) if trades else 0.0,
        "trades": len(trades),
        "avg_r": statistics.fmean(t.r for t in trades) if trades else 0.0,
        "t_stat": newey_west_t(rets, lags=5),
        "skew": _moment(rets, 3),
        "kurtosis": _moment(rets, 4) or 3.0,
    }


def _moment(xs: list[float], k: int) -> float:
    if len(xs) < 3:
        return 0.0
    m, sd = statistics.fmean(xs), statistics.pstdev(xs)
    return sum(((x - m) / sd) ** k for x in xs) / len(xs) if sd > 0 else 0.0


def deflated_sharpe_prob(m: dict, trial_sharpes: list[float]) -> float:
    """Probability the true Sharpe beats the best you'd expect from luck across all trials (Bailey & Lopez de Prado)."""
    n = m["days"]
    if n < 3:
        return 0.0
    sr = m["sharpe"] / math.sqrt(365)  # per-day
    trials = max(1, len(trial_sharpes))
    if trials > 1:
        per_day = [s / math.sqrt(365) for s in trial_sharpes]
        v = statistics.pvariance(per_day) or 1 / n
        g = 0.5772156649
        z = NormalDist().inv_cdf
        sr0 = math.sqrt(v) * ((1 - g) * z(1 - 1 / trials) + g * z(1 - 1 / (trials * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - m["skew"] * sr + (m["kurtosis"] - 1) / 4 * sr * sr
    if denom <= 0:
        return 0.0
    return NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))


def regime_breakdown(trades: list[Trade]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    total = sum(t.pnl for t in trades) or 1e-9
    for t in trades:
        r = out.setdefault(t.regime, {"trades": 0, "pnl": 0.0, "sum_r": 0.0})
        r["trades"] += 1
        r["pnl"] += t.pnl
        r["sum_r"] += t.r
    for r in out.values():
        r["avg_r"] = r.pop("sum_r") / r["trades"]
        r["pnl_share"] = r["pnl"] / total
    return out


def gate(m: dict, m_2x: dict, dsr: float, regimes: dict, g: dict) -> dict[str, dict]:
    def row(ok, value, rule):
        return {"ok": bool(ok), "value": value, "rule": rule}
    worst = min((r["avg_r"] for r in regimes.values() if r["trades"] >= 10), default=0.0)
    top_share = max((r["pnl_share"] for r in regimes.values()), default=0.0)
    return {
        "sharpe": row(m["sharpe"] > g["sharpe_min"], m["sharpe"], f"> {g['sharpe_min']}"),
        "max_drawdown": row(m["max_drawdown"] < g["max_drawdown_max"], m["max_drawdown"], f"< {g['max_drawdown_max']}"),
        "hit_rate": row(m["hit_rate"] > g["hit_rate_min"], m["hit_rate"], f"> {g['hit_rate_min']}"),
        "t_stat": row(m["t_stat"] > g["t_stat_min"], m["t_stat"], f"> {g['t_stat_min']}"),
        "trades": row(m["trades"] >= g["min_trades"], m["trades"], f">= {g['min_trades']}"),
        "sharpe_2x_costs": row(m_2x["sharpe"] > g["sharpe_2x_costs_min"], m_2x["sharpe"], f"> {g['sharpe_2x_costs_min']}"),
        "deflated_sharpe": row(dsr > g["deflated_sharpe_prob_min"], dsr, f"> {g['deflated_sharpe_prob_min']}"),
        "worst_regime": row(worst >= -0.2 and top_share <= 0.6, {"worst_avg_r": worst, "top_pnl_share": top_share},
                            "worst regime avg R >= -0.2 and no regime > 60% of P&L"),
    }


def split_periods(candles: list[Candle], oos_days: int = 180, holdout_days: int = 90):
    """Development, out-of-sample and locked holdout, in time order."""
    end = candles[-1].end
    h0 = end - holdout_days * 86_400
    o0 = h0 - oos_days * 86_400
    dev = [c for c in candles if c.end <= o0]
    oos = [c for c in candles if o0 - WINDOW * c.seconds < c.end <= h0]  # include warm-up history
    hold = [c for c in candles if h0 - WINDOW * c.seconds < c.end]
    return dev, oos, hold


def trades_as_dicts(trades: list[Trade]) -> list[dict]:
    return [asdict(t) for t in trades]
