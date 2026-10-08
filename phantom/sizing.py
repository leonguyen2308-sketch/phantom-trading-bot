"""Position sizing: capped fractional Kelly from a calibrated P(win), then hard caps."""

from __future__ import annotations

from dataclasses import dataclass, field

from phantom.config import RiskLimits


@dataclass(frozen=True)
class KellyConfig:
    p_cut: float
    p_full: float
    max_multiplier: float = 0.25


@dataclass
class Size:
    qty: float
    notional: float
    risk_usd: float
    risk_pct: float
    kelly_risk_pct: float
    capped_by: list[str] = field(default_factory=list)


def kelly_fraction(p: float, b: float) -> float:
    return p - (1 - p) / b


def kelly_multiplier(p: float, k: KellyConfig) -> float:
    """0 below the cutoff, rising linearly to max_multiplier (quarter Kelly) at p_full."""
    if p < k.p_cut:
        return 0.0
    return min(1.0, (p - k.p_cut) / (k.p_full - k.p_cut)) * k.max_multiplier


def position_size(*, equity: float, entry: float, stop: float, p: float, b: float, kelly: KellyConfig,
                  limits: RiskLimits, kelly_enabled: bool, fixed_risk_pct: float) -> Size:
    if fixed_risk_pct > limits.max_risk_per_trade_pct:
        raise ValueError("fixed_risk_pct is above the hard per-trade risk limit")
    if entry <= stop:
        raise ValueError("stop must be below entry for a long")
    kelly_risk = max(0.0, kelly_multiplier(p, kelly) * kelly_fraction(p, b))
    capped: list[str] = []
    if kelly_enabled:
        risk_pct = kelly_risk
        if risk_pct > limits.max_risk_per_trade_pct:
            risk_pct = limits.max_risk_per_trade_pct
            capped.append("max_risk_per_trade_pct")
    else:
        risk_pct = fixed_risk_pct
    if risk_pct <= 0:
        return Size(0.0, 0.0, 0.0, 0.0, kelly_risk, ["below_cutoff"])
    qty = equity * risk_pct / (entry - stop)
    for name, cap in (("max_position_pct", equity * limits.max_position_pct), ("max_position_usd", limits.max_position_usd)):
        if qty * entry > cap:
            qty = cap / entry
            capped.append(name)
    risk_usd = qty * (entry - stop)
    return Size(qty=qty, notional=qty * entry, risk_usd=risk_usd, risk_pct=risk_usd / equity, kelly_risk_pct=kelly_risk, capped_by=capped)
