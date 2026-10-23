"""config/desk.yaml into typed settings. Unknown or missing keys are errors, not silent defaults."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from desk.plan import PlanRules


@dataclass(frozen=True)
class Scan:
    interval_seconds: float
    organic_every: int
    dexscreener_every: int
    max_universe: int
    deep_checks_per_sweep: int
    recheck_killed_minutes: float
    recheck_passed_minutes: float
    extra_jupiter_lists: list  # more discovery lists, "kind/window", pulled every extra_lists_every sweeps
    extra_lists_every: int
    gecko_discovery_every: int  # GeckoTerminal's Solana trending and new pools, every N sweeps (0 = off)


@dataclass(frozen=True)
class Gates:
    min_age_minutes: float
    max_age_hours: float
    min_liquidity_usd: float
    min_market_cap_usd: float
    max_market_cap_usd: float
    min_txns_1h: int
    min_buy_sell_ratio_1h: float
    max_price_change_1h_pct: float
    min_price_change_1h_pct: float
    min_organic_score: float
    max_jupiter_top_holders_pct: float
    max_dev_balance_pct: float
    min_vol_to_mcap_1h: float
    max_vol_to_mcap_1h: float


@dataclass(frozen=True)
class HolderRules:
    require_mint_authority_revoked: bool
    require_freeze_authority_revoked: bool
    blocked_token2022_extensions: list
    max_top1_pct: float
    max_top10_pct: float
    fresh_wallet_hours: float
    max_fresh_share_pct: float
    max_rugcheck_risk: float


@dataclass(frozen=True)
class Depth:
    max_price_impact_pct: float
    max_round_trip_loss_pct: float
    max_slippage_tolerance_pct: float
    max_requote_move_pct: float


@dataclass(frozen=True)
class Window:
    max_drop_from_high_pct: float
    max_return_30m_pct: float
    min_candles: int


@dataclass(frozen=True)
class DeskCfg:
    enabled: bool
    model: str
    effort_analyst: str
    effort_skeptic: str
    news: bool
    max_tokens: int
    daily_budget_usd: float
    max_reviews_per_hour: int
    min_conviction: float
    allowed_setups: list
    allowed_windows: list
    when_unavailable: str
    price_in_per_mtok: float
    price_out_per_mtok: float
    web_search_usd_per_call: float


@dataclass(frozen=True)
class Risk:
    position_pct: float
    min_position_usd: float
    max_position_usd: float
    max_open_positions: int
    max_exposure_pct: float
    daily_loss_limit_pct: float
    pause_after_losses: int
    pause_hours: float
    max_drawdown_pct: float
    max_consecutive_api_errors: int
    min_sol_for_fees: float


@dataclass(frozen=True)
class Paper:
    slippage_bps: float


@dataclass(frozen=True)
class Track:
    enabled: bool
    max_signal_age_seconds: float
    exit_when_wallet_sells: bool


@dataclass(frozen=True)
class Hot:
    """HOT MODE: skip the strategy filters and buy coins that many people are looking at, in a market-cap band. Only
    what makes a coin impossible to trade or an obvious trap stays (authorities, a route to sell it back)."""
    enabled: bool
    min_market_cap_usd: float
    max_market_cap_usd: float
    min_liquidity_usd: float
    min_age_minutes: float
    max_age_hours: float
    min_txns_1h: int
    min_attention_signals: int  # how many of: GeckoTerminal trending, Jupiter trending, newly listed, boosted, very active
    jupiter_rank_limit: int  # a place on Jupiter's trending lists counts as attention up to this rank
    active_vol_to_mcap_1h: float  # "very active": an hour's volume at least this multiple of market cap
    allow_bonding_curve: bool  # pump.fun coins still on their curve (where 20-50K market caps live)
    require_authorities_revoked: bool
    max_price_impact_pct: float
    max_round_trip_loss_pct: float
    max_slippage_tolerance_pct: float


@dataclass(frozen=True)
class DeskConfig:
    scan: Scan
    gates: Gates
    holders: HolderRules
    depth: Depth
    window: Window
    desk: DeskCfg
    plan: PlanRules
    risk: Risk
    paper: Paper
    track: Track
    hot: Hot


def _build(cls, data: dict, section: str):
    names = {f.name for f in fields(cls)}
    extra, missing = set(data) - names, names - set(data)
    if extra or missing:
        raise ValueError(f"config/desk.yaml [{section}]: unknown {sorted(extra)} / missing {sorted(missing)}")
    return cls(**data)


def load_config(path: str | Path = "config/desk.yaml") -> DeskConfig:
    d = yaml.safe_load(Path(path).read_text()) or {}
    plan = dict(d.get("plan") or {})
    if "ladder_pct" in plan:
        plan["ladder_pct"] = tuple(float(x) for x in plan["ladder_pct"])
    c = DeskConfig(scan=_build(Scan, d.get("scan") or {}, "scan"), gates=_build(Gates, d.get("gates") or {}, "gates"),
                   holders=_build(HolderRules, d.get("holders") or {}, "holders"), depth=_build(Depth, d.get("depth") or {}, "depth"),
                   window=_build(Window, d.get("window") or {}, "window"), desk=_build(DeskCfg, d.get("desk") or {}, "desk"),
                   plan=_build(PlanRules, plan, "plan"), risk=_build(Risk, d.get("risk") or {}, "risk"),
                   paper=_build(Paper, d.get("paper") or {}, "paper"), track=_build(Track, d.get("track") or {}, "track"),
                   hot=_build(Hot, d.get("hot") or {}, "hot"))
    if c.hot.min_market_cap_usd > c.hot.max_market_cap_usd:
        raise ValueError("hot.min_market_cap_usd is above hot.max_market_cap_usd")
    if c.desk.when_unavailable not in ("skip", "rules_only"):
        raise ValueError("desk.when_unavailable must be skip or rules_only")
    if not 0 < c.risk.position_pct <= 20:
        raise ValueError("risk.position_pct must be between 0 and 20")
    if c.risk.min_position_usd > c.risk.max_position_usd:
        raise ValueError("risk.min_position_usd is above risk.max_position_usd")
    if c.plan.ladder_fraction * len(c.plan.ladder_pct) > 1:
        raise ValueError("plan: the ladder sells more than the whole position")
    return c
