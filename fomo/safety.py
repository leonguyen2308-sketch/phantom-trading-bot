"""Checks before every copy. Most memecoins a trader buys are fine for them and fatal for a follower
who arrives seconds later: these catch the rugs, honeypots and thin pools we can see in advance."""

from __future__ import annotations

from fomo.brokers import Quote
from fomo.chain import MintInfo
from fomo.config import Safety
from fomo.market import TokenStats


def token_vetoes(info: MintInfo, stats: TokenStats | None, s: Safety) -> list[str]:
    v: list[str] = []
    if s.require_mint_authority_revoked and info.mint_authority:
        v.append("creator can still mint more tokens")
    if s.require_freeze_authority_revoked and info.freeze_authority:
        v.append("creator can freeze holders' tokens")
    blocked = set(s.blocked_token2022_extensions or [])
    bad = sorted({e.get("extension") for e in info.extensions if e.get("extension") in blocked})
    if bad:
        v.append(f"Token-2022 extension {', '.join(bad)}")
    if stats is None:
        v.append("no trading pool found")
        return v
    if stats.liquidity_usd < s.min_liquidity_usd and stats.liquidity_usd == 0 and stats.dex == "pumpfun":
        v.append("still on the pump.fun bonding curve (no pool yet; set min_liquidity_usd: 0 to allow)")
    elif stats.liquidity_usd < s.min_liquidity_usd:
        v.append(f"liquidity ${stats.liquidity_usd:,.0f} < ${s.min_liquidity_usd:,.0f}")
    if stats.market_cap_usd < s.min_market_cap_usd:
        v.append(f"market cap ${stats.market_cap_usd:,.0f} < ${s.min_market_cap_usd:,.0f}")
    if s.max_market_cap_usd and stats.market_cap_usd > s.max_market_cap_usd:
        v.append(f"market cap ${stats.market_cap_usd:,.0f} > ${s.max_market_cap_usd:,.0f}")
    return v


def quote_vetoes(buy: Quote, sell_back: Quote | None, decimals: int, trader_price: float, s: Safety,
                 max_chase_pct: float) -> list[str]:
    v: list[str] = []
    if buy.impact_pct > s.max_price_impact_pct:
        v.append(f"price impact {buy.impact_pct:.1f}% > {s.max_price_impact_pct:g}%")
    ours = buy.usd / (buy.raw / 10 ** decimals) if buy.raw else float("inf")
    if trader_price > 0:
        chase = (ours / trader_price - 1) * 100
        if chase > max_chase_pct:
            v.append(f"price already {chase:.0f}% above the trader's entry")
    if s.check_sell_route:
        if sell_back is None:
            v.append("no route to sell it back (possible honeypot)")
        else:
            loss = (1 - sell_back.usd / buy.usd) * 100 if buy.usd else 100
            if loss > s.max_round_trip_loss_pct:
                v.append(f"buying and selling straight back loses {loss:.0f}% (hidden tax or thin pool)")
    return v
