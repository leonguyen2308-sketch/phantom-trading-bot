"""The numeric snapshot of one token: the only thing the Claude seats see. Every value is computed here, in code.

No names, tickers, descriptions or links go in (attacker-controlled text). Missing data is None, never guessed.
"""

from __future__ import annotations

import math

LEGEND = {
    "age_min": "minutes since the token's first DEX pool opened",
    "mcap_usd": "market capitalisation in USD",
    "liquidity_usd": "USD liquidity across the token's pools",
    "liq_to_mcap": "liquidity divided by market cap (low = thin pool for its size)",
    "holder_count": "number of wallets holding the token",
    "holder_change_1h_pct": "change in holder count over the last hour, percent",
    "organic_score": "Jupiter's 0-100 score of how organic (non-bot) the trading is",
    "organic_buy_share_1h": "share of the last hour's buy volume Jupiter counts as organic, 0-1",
    "traders_1h": "distinct wallets that traded in the last hour",
    "vol_5m_usd": "traded volume in the last 5 minutes, USD",
    "vol_1h_usd": "traded volume in the last hour, USD",
    "vol_6h_usd": "traded volume in the last 6 hours, USD",
    "vol_accel": "last 5 minutes' volume x 12 divided by the last hour's volume (above 1 = accelerating)",
    "vol_to_mcap_1h": "last hour's volume divided by market cap",
    "buys_5m": "buy transactions in the last 5 minutes",
    "sells_5m": "sell transactions in the last 5 minutes",
    "buys_1h": "buy transactions in the last hour",
    "sells_1h": "sell transactions in the last hour",
    "buy_sell_ratio_1h": "buys_1h divided by sells_1h",
    "price_change_5m_pct": "price change over 5 minutes, percent",
    "price_change_1h_pct": "price change over 1 hour, percent",
    "price_change_6h_pct": "price change over 6 hours, percent",
    "price_change_24h_pct": "price change over 24 hours, percent",
    "atr_5m_pct": "average 5-minute candle range over the last hour, percent of price",
    "drop_from_high_pct": "how far the last close is below the highest high of the candles shown, percent",
    "minutes_since_high": "minutes since that high",
    "return_30m_pct": "price change over the last 30 minutes of candles, percent",
    "return_2h_pct": "price change over the last 2 hours of candles, percent",
    "up_candles_last_12": "how many of the last 12 five-minute candles closed up",
    "top1_pct": "share of supply held by the largest real wallet (pools and burns excluded), percent",
    "top10_pct": "share of supply held by the 10 largest real wallets, percent",
    "fresh_share_pct": "share of supply held by wallets created within the configured window, among the top 10",
    "dev_balance_pct": "share of supply still held by the creator, percent (Jupiter)",
    "dev_mints": "how many tokens the creator has launched (Jupiter); high = serial launcher",
    "rugcheck_risk": "RugCheck normalised risk score, higher is riskier (null = unavailable)",
    "boosted": "1 if the token is currently paying DexScreener for promotion, else 0",
    "trending_rank_gecko": "position (1 = hottest) on GeckoTerminal's Solana trending pools, a ranking of the pools people are viewing and trading; null = not listed. Attention is not quality: most trending memecoins are already late",
    "trending_rank_jupiter": "best position (1 = hottest) on Jupiter's trending lists (5 minutes, 1 hour); null = not listed. Same caveat: attention is not quality",
    "new_listing": "1 if GeckoTerminal lists the token's pool among Solana's newest pools, else 0",
    "social_links": "number of website and social links listed for the token",
    "impact_pct": "price impact of our buy ticket in a real Jupiter quote, percent",
    "round_trip_loss_pct": "percent lost by buying our ticket and selling it straight back (quotes)",
}


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _get(d: dict | None, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def market_fields(jt: dict | None, dx: dict | None, now: float) -> dict:
    """Fields from a Jupiter token record and the deepest DexScreener pair. Either may be missing."""
    created = None
    if _get(jt, "firstPool", "createdAt"):
        from datetime import datetime
        try:
            created = datetime.fromisoformat(str(jt["firstPool"]["createdAt"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            created = None
    if created is None and _f(_get(dx, "pairCreatedAt")):
        created = _f(dx["pairCreatedAt"]) / 1000
    mcap = _f(_get(dx, "marketCap")) or _f(_get(jt, "mcap")) or _f(_get(dx, "fdv"))
    liq = _f(_get(dx, "_total_liquidity_usd")) or _f(_get(dx, "liquidity", "usd")) or _f(_get(jt, "liquidity"))
    v5, v1, v6 = (_f(_get(dx, "volume", k)) for k in ("m5", "h1", "h6"))
    b5, s5 = _f(_get(dx, "txns", "m5", "buys")), _f(_get(dx, "txns", "m5", "sells"))
    b1, s1 = _f(_get(dx, "txns", "h1", "buys")), _f(_get(dx, "txns", "h1", "sells"))
    buy_vol, org_buy = _f(_get(jt, "stats1h", "buyVolume")), _f(_get(jt, "stats1h", "buyOrganicVolume"))
    socials = len(_get(dx, "info", "socials") or []) + len(_get(dx, "info", "websites") or [])
    return {
        "age_min": round((now - created) / 60, 1) if created else None,
        "mcap_usd": mcap,
        "liquidity_usd": liq,
        "liq_to_mcap": round(liq / mcap, 4) if liq and mcap else None,
        "holder_count": _f(_get(jt, "holderCount")),
        "holder_change_1h_pct": _f(_get(jt, "stats1h", "holderChange")),
        "organic_score": _f(_get(jt, "organicScore")),
        "organic_buy_share_1h": round(org_buy / buy_vol, 3) if buy_vol and org_buy is not None else None,
        "traders_1h": _f(_get(jt, "stats1h", "numTraders")),
        "vol_5m_usd": v5, "vol_1h_usd": v1, "vol_6h_usd": v6,
        "vol_accel": round(v5 * 12 / v1, 3) if v5 is not None and v1 else None,
        "vol_to_mcap_1h": round(v1 / mcap, 4) if v1 is not None and mcap else None,
        "buys_5m": b5, "sells_5m": s5, "buys_1h": b1, "sells_1h": s1,
        "buy_sell_ratio_1h": round(b1 / s1, 3) if b1 is not None and s1 else None,
        "price_change_5m_pct": _f(_get(dx, "priceChange", "m5")),
        "price_change_1h_pct": _f(_get(dx, "priceChange", "h1")),
        "price_change_6h_pct": _f(_get(dx, "priceChange", "h6")),
        "price_change_24h_pct": _f(_get(dx, "priceChange", "h24")),
        "dev_balance_pct": _f(_get(jt, "audit", "devBalancePercentage")),
        "dev_mints": _f(_get(jt, "audit", "devMints")),
        "boosted": 1.0 if (_f(_get(dx, "boosts", "active")) or 0) > 0 else 0.0,
        "social_links": float(socials) if dx else None,
    }


def candle_fields(candles: list[list[float]], now: float) -> dict:
    """From 5-minute candles [ts, o, h, l, c, v] oldest first."""
    if not candles:
        return {"atr_5m_pct": None, "drop_from_high_pct": None, "minutes_since_high": None, "return_30m_pct": None,
                "return_2h_pct": None, "up_candles_last_12": None}
    last = candles[-12:]
    close = candles[-1][4]
    ranges = [(c[2] - c[3]) / c[4] * 100 for c in last if c[4] > 0]
    hi_idx = max(range(len(candles)), key=lambda i: candles[i][2])
    hi = candles[hi_idx][2]

    def ret(n):
        if len(candles) <= n or candles[-1 - n][4] <= 0:
            return None
        return round((close / candles[-1 - n][4] - 1) * 100, 2)

    return {
        "atr_5m_pct": round(sum(ranges) / len(ranges), 3) if ranges else None,
        "drop_from_high_pct": round((hi - close) / hi * 100, 2) if hi > 0 else None,
        "minutes_since_high": round((now - candles[hi_idx][0]) / 60, 1),
        "return_30m_pct": ret(6),
        "return_2h_pct": ret(24),
        "up_candles_last_12": float(sum(1 for c in last if c[4] > c[1])),
    }


def snapshot(market: dict, candles: dict, holders: dict, depth: dict) -> dict:
    snap = {**market, **candles, **holders, **depth}
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in snap.items() if k in LEGEND}
