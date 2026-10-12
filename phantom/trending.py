"""Post trending coins to X: contract address (CA), market cap, 24h volume, price change.

Data: GeckoTerminal's public trending pools (no key; ~30 calls/min). Each coin is posted at most
once per REPOST_HOURS. This only posts information; it never trades.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API = "https://api.geckoterminal.com/api/v2"
REPOST_HOURS = 24
# Majors and stables trend all the time and aren't news.
SKIP_SYMBOLS = {"SOL", "WSOL", "ETH", "WETH", "BTC", "WBTC", "CBBTC", "USDC", "USDT", "DAI", "BNB", "WBNB", "USD1", "PYUSD"}
NETWORK_NAMES = {"solana": "Solana", "eth": "Ethereum", "base": "Base", "bsc": "BNB Chain", "arbitrum": "Arbitrum"}


def fetch_trending(http: httpx.Client, network: str = "") -> list[dict]:
    """Trending pools -> list of coins, best first. network='' means all chains, else e.g. 'solana'."""
    path = f"/networks/{network}/trending_pools" if network else "/networks/trending_pools"
    r = http.get(API + path, params={"include": "base_token"}, headers={"Accept": "application/json"})
    r.raise_for_status()
    body = r.json()
    tokens = {t["id"]: t.get("attributes", {}) for t in body.get("included", []) if t.get("type") == "token"}
    coins = []
    for pool in body.get("data", []):
        a = pool.get("attributes", {})
        rel = pool.get("relationships", {})
        tok_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
        tok = tokens.get(tok_id, {})
        net = tok_id.split("_", 1)[0] if "_" in tok_id else network
        ca = tok.get("address") or (tok_id.split("_", 1)[1] if "_" in tok_id else "")
        if not ca:
            continue
        coins.append({
            "network": net, "ca": ca, "name": tok.get("name") or a.get("name", ""), "symbol": (tok.get("symbol") or "").upper(),
            "mcap": _num(a.get("market_cap_usd")), "fdv": _num(a.get("fdv_usd")),
            "vol24": _num((a.get("volume_usd") or {}).get("h24")), "chg24": _num((a.get("price_change_percentage") or {}).get("h24")),
            "liq": _num(a.get("reserve_in_usd")), "price": _num(a.get("base_token_price_usd")),
            "url": f"https://www.geckoterminal.com/{net}/pools/{a.get('address', '')}",
        })
    return coins


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def money(v: float | None) -> str:
    if v is None:
        return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suf}"
    return f"${v:,.0f}"


def format_post(c: dict, hashtags: str = "") -> str:
    chain = NETWORK_NAMES.get(c["network"], c["network"].capitalize())
    head = f"🔥 Trending on {chain}: ${c['symbol']}" + (f" ({c['name']})" if c["name"] and c["name"].upper() != c["symbol"] else "")
    mc = f"MC: {money(c['mcap'])}" if c["mcap"] else f"FDV: {money(c['fdv'])}"
    lines = [
        head[:80],
        f"CA: {c['ca']}",
        f"{mc} | Vol 24h: {money(c['vol24'])}",
        f"24h: {c['chg24']:+.1f}% | Liq: {money(c['liq'])}" if c["chg24"] is not None else f"Liq: {money(c['liq'])}",
        f"📊 {c['url']}",
        "DYOR, not financial advice.",
    ]
    if hashtags:
        lines.append(hashtags)
    text = "\n".join(lines)
    # X counts every link as 23 characters.
    if len(text) - len(c["url"]) + 23 > 280 and hashtags:
        text = "\n".join(lines[:-1])
    return text


def pick(coins: list[dict], posted: dict[str, float], now: float, n: int, min_vol: float) -> list[dict]:
    out = []
    for c in coins:
        key = f"{c['network']}:{c['ca']}"
        if c["symbol"] in SKIP_SYMBOLS or (c["vol24"] or 0) < min_vol:
            continue
        if now - posted.get(key, float("-inf")) < REPOST_HOURS * 3600 or any(o["ca"] == c["ca"] for o in out):
            continue
        out.append(c)
        if len(out) >= n:
            break
    return out


def run(poster, state: Path, network: str = "", every_min: float = 60, per_run: int = 1, min_vol: float = 50_000,
        hashtags: str = "", once: bool = False, dry_run: bool = False, http: httpx.Client | None = None,
        clock=time.time, sleep=time.sleep) -> None:
    http = http or httpx.Client(timeout=20)
    while True:
        try:
            posted = json.loads(state.read_text()) if state.exists() else {}
            now = clock()
            posted = {k: t for k, t in posted.items() if now - t < REPOST_HOURS * 3600}
            for c in pick(fetch_trending(http, network), posted, now, per_run, min_vol):
                text = format_post(c, hashtags)
                if dry_run:
                    print(text, "\n" + "-" * 40)
                    continue
                poster.post(text)
                log.info("Posted %s %s to X", c["symbol"], c["ca"])
                posted[f"{c['network']}:{c['ca']}"] = now
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(json.dumps(posted))
        except Exception as e:  # keep going; next round may work
            log.warning("trending round failed: %s: %s", type(e).__name__, e)
        if once:
            return
        sleep(every_min * 60)
