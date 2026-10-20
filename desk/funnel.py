"""The funnel's gates, cheapest first. Each returns the reasons a token dies there (empty list = it passes).

G1 market numbers in hand  ->  G2 on-chain facts  ->  G3 a real quote for our ticket  ->  G4 where it is in its move.
A safety fact that can't be read is a kill, never a pass: missing data only lets a token through where the check
is about opportunity (organic score), not about danger.
`checks()` folds them into the six named checks the dashboard shows (liquidity depth, holder spread, volume vs
mcap, rug signature, fresh wallets, entry window), each PASS / FAIL / ? (unknown).
"""

from __future__ import annotations

GATES = ["universe", "market", "onchain", "depth", "window", "desk", "filled"]


def _lt(v, lo):
    return v is not None and v < lo


def _gt(v, hi):
    return v is not None and v > hi


def g1_market(m: dict, dex_id: str, jt: dict | None, g) -> list[str]:
    why = []
    if not jt:
        why.append("no Jupiter record (audit unknown)")
    if dex_id == "pumpfun" or (m.get("liquidity_usd") or 0) <= 0:
        why.append("still on the pump.fun bonding curve or no pool")
    if m.get("age_min") is None:
        why.append("pool age unknown")
    elif m["age_min"] < g.min_age_minutes:
        why.append(f"only {m['age_min']:.0f} min old")
    elif m["age_min"] > g.max_age_hours * 60:
        why.append(f"{m['age_min'] / 60:.0f}h old")
    if _lt(m.get("liquidity_usd"), g.min_liquidity_usd):
        why.append(f"liquidity ${m['liquidity_usd']:,.0f}")
    mc = m.get("mcap_usd")
    if mc is None or mc < g.min_market_cap_usd or mc > g.max_market_cap_usd:
        why.append(f"market cap {'unknown' if mc is None else f'${mc:,.0f}'} outside the band")
    tx = (m.get("buys_1h") or 0) + (m.get("sells_1h") or 0)
    if tx < g.min_txns_1h:
        why.append(f"{tx:.0f} trades in the last hour")
    if not m.get("sells_1h"):
        why.append("no sells in the last hour (honeypot?)")
    if _lt(m.get("buy_sell_ratio_1h"), g.min_buy_sell_ratio_1h):
        why.append(f"sellers outnumber buyers ({m['buy_sell_ratio_1h']:.2f})")
    pc = m.get("price_change_1h_pct")
    if _gt(pc, g.max_price_change_1h_pct):
        why.append(f"already +{pc:.0f}% this hour (chasing)")
    if _lt(pc, g.min_price_change_1h_pct):
        why.append(f"dumping {pc:.0f}% this hour")
    if _lt(m.get("organic_score"), g.min_organic_score):
        why.append(f"organic score {m['organic_score']:.0f}")
    vm = m.get("vol_to_mcap_1h")
    if vm is None:
        why.append("hourly volume unknown")
    if _lt(vm, g.min_vol_to_mcap_1h):
        why.append("almost no trading for its size")
    if _gt(vm, g.max_vol_to_mcap_1h):
        why.append(f"hourly volume {vm:.1f}x market cap (wash trading?)")
    audit = (jt or {}).get("audit") or {}
    if audit.get("isSus"):
        why.append("Jupiter flags it as suspicious")
    if audit.get("mintAuthorityDisabled") is False:
        why.append("creator can still mint")
    if audit.get("freezeAuthorityDisabled") is False:
        why.append("creator can freeze holders")
    if _gt(audit.get("topHoldersPercentage"), g.max_jupiter_top_holders_pct):
        why.append(f"top holders own {audit['topHoldersPercentage']:.0f}%")
    if _gt(m.get("dev_balance_pct"), g.max_dev_balance_pct):
        why.append(f"creator still holds {m['dev_balance_pct']:.0f}%")
    return why


def g2_onchain(info, h: dict, rug: dict | None, r) -> list[str]:
    """`info` is a fomo.chain.MintInfo; `h` has top1_pct, top10_pct, fresh_share_pct."""
    why = []
    if r.require_mint_authority_revoked and info.mint_authority:
        why.append("creator can still mint (on-chain)")
    if r.require_freeze_authority_revoked and info.freeze_authority:
        why.append("creator can freeze holders (on-chain)")
    bad = sorted({e.get("extension") for e in info.extensions if e.get("extension") in set(r.blocked_token2022_extensions or [])})
    if bad:
        why.append(f"Token-2022 extension {', '.join(bad)}")
    if _gt(h.get("top1_pct"), r.max_top1_pct):
        why.append(f"one wallet holds {h['top1_pct']:.1f}%")
    if _gt(h.get("top10_pct"), r.max_top10_pct):
        why.append(f"top 10 wallets hold {h['top10_pct']:.0f}%")
    if _gt(h.get("fresh_share_pct"), r.max_fresh_share_pct):
        why.append(f"fresh wallets hold {h['fresh_share_pct']:.0f}%")
    risk = h.get("rugcheck_risk")
    if _gt(risk, r.max_rugcheck_risk):
        why.append(f"RugCheck risk {risk:.0f}")
    if rug and rug.get("rugged"):
        why.append("RugCheck marks it rugged")
    for risk in (rug or {}).get("risks") or []:
        if isinstance(risk, dict) and risk.get("level") == "danger":
            why.append(f"RugCheck: {str(risk.get('name') or 'danger')[:40]}")
    return why


def g2_unknowns(h: dict, rug: dict | None) -> list[str]:
    """Safety facts that could not be read. Not a verdict on the token, so it is only set aside for a while."""
    why = []
    if rug is None or h.get("rugcheck_risk") is None:
        why.append("RugCheck unavailable (unknown is not a pass)")
    if h.get("top10_pct") is None:
        why.append("top holders unknown")
    return why


def g3_depth(buy, sell, decimals: int, d) -> tuple[dict, list[str]]:
    """`buy`/`sell` are fomo.brokers.Quote (sell may be None: no route back)."""
    fields = {"impact_pct": round(buy.impact_pct, 3), "round_trip_loss_pct": None}
    why = []
    if buy.impact_pct > d.max_price_impact_pct:
        why.append(f"price impact {buy.impact_pct:.1f}%")
    if buy.min_out and buy.raw:
        tol = (1 - buy.min_out / buy.raw) * 100
        if tol > d.max_slippage_tolerance_pct:
            why.append(f"quote allows {tol:.1f}% slippage (sandwich bait)")
    if sell is None:
        why.append("no route to sell it back (honeypot?)")
    else:
        loss = (1 - sell.usd / buy.usd) * 100 if buy.usd else 100.0
        fields["round_trip_loss_pct"] = round(loss, 3)
        if loss > d.max_round_trip_loss_pct:
            why.append(f"buying and selling back loses {loss:.1f}%")
    return fields, why


def g4_window(c: dict, n_candles: int, w) -> list[str]:
    why = []
    if n_candles < w.min_candles:
        why.append(f"only {n_candles} candles of history")
    if _gt(c.get("drop_from_high_pct"), w.max_drop_from_high_pct):
        why.append(f"{c['drop_from_high_pct']:.0f}% below its high (fading)")
    if _gt(c.get("return_30m_pct"), w.max_return_30m_pct):
        why.append(f"+{c['return_30m_pct']:.0f}% in 30 min (parabolic)")
    return why


CHECKS = ["liquidity depth", "holder spread", "volume vs mcap", "rug signature", "fresh wallets", "entry window"]


def checks(snap: dict, kills: dict[str, list[str]]) -> dict[str, str]:
    """Video 2's six checks, as PASS / FAIL / ? from what the gates found."""
    def has(gate, *words):
        return any(any(w in r for w in words) for r in kills.get(gate, []))

    def st(fail: bool, known: bool) -> str:
        return "FAIL" if fail else ("PASS" if known else "?")

    return {
        "liquidity depth": st(has("market", "liquidity", "bonding") or has("depth", "impact", "back", "slippage"),
                              snap.get("round_trip_loss_pct") is not None),
        "holder spread": st(has("onchain", "wallet holds", "top 10") or has("market", "top holders", "creator still holds"),
                            snap.get("top10_pct") is not None),
        "volume vs mcap": st(has("market", "wash", "no trading"), snap.get("vol_to_mcap_1h") is not None),
        "rug signature": st(has("onchain", "mint", "freeze", "Token-2022", "RugCheck") or has("market", "suspicious", "mint", "freeze"),
                            snap.get("rugcheck_risk") is not None),
        "fresh wallets": st(has("onchain", "fresh"), snap.get("fresh_share_pct") is not None),
        "entry window": st(has("window", "fading", "parabolic", "candles") or has("market", "chasing", "dumping"),
                           snap.get("drop_from_high_pct") is not None),
    }
