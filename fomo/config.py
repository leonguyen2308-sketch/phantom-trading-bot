"""config/fomo.yaml, read into typed settings. Unknown or missing keys are an error, not a silent default."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CopyRules:
    copy_ratio: float
    min_trade_usd: float
    max_trade_usd: float
    exit_with_trader: bool
    fire_after_losses: int
    lineup_size: int
    refresh_hours: float
    fire_cooldown_days: float
    only_first_buy: bool
    max_signal_age_seconds: float
    max_chase_pct: float
    rebuy_cooldown_hours: float


@dataclass(frozen=True)
class Exits:
    stop_loss_pct: float | None
    take_profit_pct: float | None
    max_hold_hours: float | None


@dataclass(frozen=True)
class Safety:
    min_liquidity_usd: float
    min_market_cap_usd: float
    max_market_cap_usd: float | None
    require_mint_authority_revoked: bool
    require_freeze_authority_revoked: bool
    blocked_token2022_extensions: list
    max_price_impact_pct: float
    check_sell_route: bool
    max_round_trip_loss_pct: float


@dataclass(frozen=True)
class Limits:
    max_open_positions: int
    max_position_pct: float
    max_exposure_pct: float
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    max_consecutive_api_errors: int
    min_sol_for_fees: float


@dataclass(frozen=True)
class Paper:
    slippage_bps: float


@dataclass(frozen=True)
class FomoConfig:
    copy: CopyRules
    exits: Exits
    safety: Safety
    limits: Limits
    paper: Paper


def _build(cls, data: dict, section: str):
    names = {f.name for f in fields(cls)}
    extra, missing = set(data) - names, names - set(data)
    if extra or missing:
        raise ValueError(f"config/fomo.yaml [{section}]: unknown {sorted(extra)} / missing {sorted(missing)}")
    return cls(**data)


def load_config(path: str | Path = "config/fomo.yaml") -> FomoConfig:
    d = yaml.safe_load(Path(path).read_text()) or {}
    c = FomoConfig(copy=_build(CopyRules, d.get("copy") or {}, "copy"), exits=_build(Exits, d.get("exits") or {}, "exits"),
                   safety=_build(Safety, d.get("safety") or {}, "safety"), limits=_build(Limits, d.get("limits") or {}, "limits"),
                   paper=_build(Paper, d.get("paper") or {}, "paper"))
    if not 0 < c.copy.copy_ratio <= 1:
        raise ValueError("copy_ratio must be between 0 and 1")
    if c.copy.min_trade_usd > c.copy.max_trade_usd:
        raise ValueError("min_trade_usd is above max_trade_usd")
    if c.copy.fire_after_losses < 1:
        raise ValueError("fire_after_losses must be at least 1")
    return c
