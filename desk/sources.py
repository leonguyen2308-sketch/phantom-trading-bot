"""Where the desk's numbers come from. All free tiers; every host behind its own rate limit.

- Jupiter Tokens API v2 (api.jup.ag/tokens/v2): discovery lists and a 100-mint batch with mcap, liquidity,
  holder count, organic score, 5m/1h/6h/24h stats and an audit block. The free key allows about 1 request a
  second SHARED with Jupiter quotes and prices, so every Jupiter call goes through one limiter.
- DexScreener (no key): 30-mint batches of pairs with liquidity, volume, buys/sells, pair address; discovery
  of newly profiled and boosted tokens.
- GeckoTerminal (no key, ~10 calls/min shared by IP): 1m/5m candles, for finalists only.
- RugCheck (no key, unpublished limits): the ~300-byte risk summary, finalists only; unavailable = unknown.
- Solana RPC (your Helius key): top holders and how new their wallets are.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

JUP_TOKENS = "https://api.jup.ag/tokens/v2"
DEXSCREENER = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
BURN = "1nc1nerator11111111111111111111111111111111"


class _Bucket:
    def __init__(self, rate: float, burst: float = 1.0):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, time.monotonic()
        self.lock = threading.Lock()

    def take(self, sleep=time.sleep) -> None:
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                sleep((1 - self.tokens) / self.rate)


class LimitedClient(httpx.Client):
    """httpx.Client with a token bucket per host (Jupiter's /execute has its own), and one polite retry on 429."""

    RATES = {"api.jup.ag": 0.8, "api.jup.ag/execute": 5.0, "api.dexscreener.com": 4.0,
             "api.geckoterminal.com": 0.15, "api.rugcheck.xyz": 0.5}

    def __init__(self, rates: dict | None = None, sleep=time.sleep, **kw):
        kw.setdefault("timeout", 20)
        super().__init__(**kw)
        self._sleep = sleep
        self._buckets = {k: _Bucket(v) for k, v in {**self.RATES, **(rates or {})}.items()}

    def _bucket(self, url) -> _Bucket | None:
        u = urlparse(str(url))
        if u.hostname == "api.jup.ag" and u.path.endswith("/execute"):
            return self._buckets.get("api.jup.ag/execute")
        return self._buckets.get(u.hostname or "")

    def request(self, method, url, *args, **kw):
        b = self._bucket(url)
        for attempt in range(2):
            if b:
                b.take(self._sleep)
            r = super().request(method, url, *args, **kw)
            if r.status_code != 429 or attempt:
                return r
            wait = r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset") or "2"
            try:
                wait_s = float(wait)
            except ValueError:
                wait_s = 2.0
            if wait_s > 1e9:  # an epoch timestamp, not a delay
                wait_s = max(0.0, wait_s - time.time())
            self._sleep(min(max(wait_s, 0.5), 30.0))
        return r


def _num(x, default=None):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


class JupiterTokens:
    def __init__(self, api_key: str, http: httpx.Client):
        self.http, self.headers = http, ({"x-api-key": api_key} if api_key else {})

    def _get(self, path: str, **params) -> list[dict]:
        r = self.http.get(f"{JUP_TOKENS}{path}", params=params or None, headers=self.headers)
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else []

    def recent(self) -> list[dict]:
        return self._get("/recent")

    def top(self, kind: str = "toptrending", window: str = "5m", limit: int = 100) -> list[dict]:
        return self._get(f"/{kind}/{window}", limit=limit)

    def search(self, mints: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(mints), 100):
            for t in self._get("/search", query=",".join(mints[i:i + 100])):
                if t.get("id"):
                    out[t["id"]] = t
        return out


class DexScreener:
    def __init__(self, http: httpx.Client):
        self.http = http

    def tokens(self, mints: list[str]) -> dict[str, dict]:
        """Deepest Solana pair per mint (by liquidity), plus total liquidity across its pairs."""
        out: dict[str, dict] = {}
        for i in range(0, len(mints), 30):
            r = self.http.get(f"{DEXSCREENER}/tokens/v1/solana/{','.join(mints[i:i + 30])}")
            r.raise_for_status()
            pairs: dict[str, list] = {}
            for p in r.json() or []:
                m = (p.get("baseToken") or {}).get("address")
                if p.get("chainId") == "solana" and m:
                    pairs.setdefault(m, []).append(p)
            for m, ps in pairs.items():
                best = max(ps, key=lambda p: _num((p.get("liquidity") or {}).get("usd"), 0))
                best = dict(best)
                best["_total_liquidity_usd"] = sum(_num((p.get("liquidity") or {}).get("usd"), 0) for p in ps)
                out[m] = best
        return out

    def discovered(self) -> list[str]:
        mints: list[str] = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            try:
                r = self.http.get(f"{DEXSCREENER}{path}")
                r.raise_for_status()
                mints += [x["tokenAddress"] for x in (r.json() or []) if x.get("chainId") == "solana" and x.get("tokenAddress")]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.info("DexScreener %s unavailable: %s", path, type(e).__name__)
        return list(dict.fromkeys(mints))


class GeckoTerminal:
    def __init__(self, http: httpx.Client):
        self.http = http

    def candles(self, pool: str, minutes: int = 5, limit: int = 100) -> list[list[float]]:
        """[[ts, open, high, low, close, volume_usd], ...] oldest first."""
        r = self.http.get(f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
                          params={"aggregate": minutes, "limit": limit, "currency": "usd", "include_empty_intervals": "true"},
                          headers={"accept": "application/json"})
        r.raise_for_status()
        rows = (((r.json() or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted(([float(v) for v in row[:6]] for row in rows if len(row) >= 6), key=lambda x: x[0])


class RugCheck:
    def __init__(self, http: httpx.Client, ttl: float = 900):
        self.http, self.ttl = http, ttl
        self._cache: dict[str, tuple[dict | None, float]] = {}

    def summary(self, mint: str) -> dict | None:
        hit = self._cache.get(mint)
        if hit and time.monotonic() - hit[1] < self.ttl:
            return hit[0]
        try:
            r = self.http.get(f"{RUGCHECK}/tokens/{mint}/report/summary")
            r.raise_for_status()
            s = r.json()
        except (httpx.HTTPError, ValueError):
            s = None  # unknown, never "pass"
        self._cache[mint] = (s, time.monotonic())
        return s


class Holders:
    """Top holders from RPC. Pools, bonding curves and other program accounts own their token accounts through
    off-curve addresses (PDAs); real wallets are on-curve. So off-curve owners and the burn address are left out."""

    def __init__(self, rpc, clock=time.time):
        self.rpc, self.clock = rpc, clock

    def top(self, mint: str) -> list[tuple[str, float]]:
        """[(owner, share of supply in %), ...] for real wallets among the 20 largest token accounts."""
        from solders.pubkey import Pubkey
        supply = self.rpc.call("getTokenSupply", [mint])["value"]
        total = int(supply["amount"]) or 1
        largest = self.rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])["value"]
        addrs = [a["address"] for a in largest]
        if not addrs:
            return []
        infos = self.rpc.call("getMultipleAccounts", [addrs, {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
        out: dict[str, float] = {}
        for a, info in zip(largest, infos):
            try:
                owner = info["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                continue
            if owner == BURN or not Pubkey.from_string(owner).is_on_curve():
                continue
            out[owner] = out.get(owner, 0.0) + int(a["amount"]) / total * 100
        return sorted(out.items(), key=lambda kv: -kv[1])

    def fresh_share(self, holders: list[tuple[str, float]], hours: float, check: int = 10) -> tuple[float, int]:
        """Share of supply (in %) held by wallets whose whole history is younger than `hours`, among the top `check`.

        One getSignaturesForAddress per wallet: if fewer than 100 signatures come back and the oldest is recent,
        the wallet's entire life fits in the window. Returns (share %, wallets checked)."""
        cutoff, share, n = self.clock() - hours * 3600, 0.0, 0
        for owner, pct in holders[:check]:
            sigs = self.rpc.call("getSignaturesForAddress", [owner, {"limit": 100, "commitment": "confirmed"}])
            n += 1
            if len(sigs) < 100 and sigs and (sigs[-1].get("blockTime") or 0) >= cutoff:
                share += pct
        return share, n
