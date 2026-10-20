"""Token prices (Jupiter Price API) and pool stats (DexScreener: liquidity, market cap, pool age)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from fomo.swaps import WSOL

log = logging.getLogger(__name__)

JUP_PRICE = "https://api.jup.ag/price/v3"
DEXSCREENER = "https://api.dexscreener.com/tokens/v1/solana"


@dataclass
class TokenStats:
    mint: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    pool_created_at: int  # unix seconds, 0 if unknown
    dex: str = ""  # DexScreener dexId of the deepest pool, e.g. "raydium", "pumpswap", "pumpfun"


class Prices:
    """USD prices for up to 50 mints per call. Cached for a few seconds.

    With `max_age`, a price older than that is left out instead of returned: Jupiter drops a mint from its answer
    when it can no longer price it (often right after a rug), and an old price would hide the crash."""

    def __init__(self, api_key: str, http: httpx.Client | None = None, ttl: float = 5.0, max_age: float | None = None):
        self.http = http or httpx.Client(timeout=15)
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.ttl, self.max_age = ttl, max_age
        self._cache: dict[str, tuple[float, float]] = {}

    def get(self, mints: list[str]) -> dict[str, float]:
        now = time.monotonic()
        need = [m for m in dict.fromkeys(mints) if now - self._cache.get(m, (0.0, -1e9))[1] > self.ttl]
        for i in range(0, len(need), 50):
            r = self.http.get(JUP_PRICE, params={"ids": ",".join(need[i:i + 50])}, headers=self.headers)
            r.raise_for_status()
            for m, v in (r.json() or {}).items():
                if isinstance(v, dict) and v.get("usdPrice"):
                    self._cache[m] = (float(v["usdPrice"]), now)
        return {m: self._cache[m][0] for m in mints
                if m in self._cache and (self.max_age is None or now - self._cache[m][1] <= self.max_age)}

    def sol_usd(self) -> float:
        p = self.get([WSOL]).get(WSOL)
        if not p:
            raise RuntimeError("no SOL price")
        return p


class DexStats:
    def __init__(self, http: httpx.Client | None = None, ttl: float = 30.0):
        self.http = http or httpx.Client(timeout=15)
        self.ttl = ttl
        self._cache: dict[str, tuple[TokenStats | None, float]] = {}

    def get(self, mint: str) -> TokenStats | None:
        hit = self._cache.get(mint)
        if hit and time.monotonic() - hit[1] < self.ttl:
            return hit[0]
        r = self.http.get(f"{DEXSCREENER}/{mint}")
        r.raise_for_status()
        pairs = [p for p in (r.json() or []) if (p.get("baseToken") or {}).get("address") == mint]
        st = None
        if pairs:
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            st = TokenStats(mint=mint, symbol=(best.get("baseToken") or {}).get("symbol") or mint[:4],
                            price_usd=float(best.get("priceUsd") or 0),
                            liquidity_usd=sum(float((p.get("liquidity") or {}).get("usd") or 0) for p in pairs),
                            market_cap_usd=float(best.get("marketCap") or best.get("fdv") or 0),
                            pool_created_at=int(best.get("pairCreatedAt") or 0) // 1000, dex=str(best.get("dexId") or ""))
        self._cache[mint] = (st, time.monotonic())
        return st
