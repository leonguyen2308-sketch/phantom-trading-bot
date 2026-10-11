"""Live meme coin data: wallet balances from a Solana RPC, prices and coin details from DexScreener."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

import httpx

log = logging.getLogger("pansol.chain")

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 (pump.fun and newer coins)
)
DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
DEXSCREENER_BATCH = 30          # addresses per request, the API's limit
DEXSCREENER_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
MAX_PRICED = 1000               # coins looked up per wallet; wallets full of airdropped spam stop here
MAX_SHOWN = 300                 # coins listed per wallet, most valuable first


class ChainError(Exception):
    """The Solana RPC could not be reached or refused the request."""


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _https(url) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _coin(pair: dict) -> dict:
    base = pair.get("baseToken") or {}
    return {
        "name": str(base.get("name") or "")[:80],
        "symbol": str(base.get("symbol") or "")[:24],
        "price_usd": _num(pair.get("priceUsd")),
        "change_24h": _num((pair.get("priceChange") or {}).get("h24")),
        "market_cap": _num(pair.get("marketCap") if pair.get("marketCap") is not None else pair.get("fdv")),
        "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
        "volume_24h": _num((pair.get("volume") or {}).get("h24")),
        "image": _https((pair.get("info") or {}).get("imageUrl")),
        "url": _https(pair.get("url")),
    }


class Chain:
    def __init__(
        self,
        rpc_url: str,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        price_ttl: float = 30,
        no_market_ttl: float = 600,
        balance_ttl: float = 20,
        outage_cooldown: float = 30,
    ):
        self.rpc_url = rpc_url
        self.http = http or httpx.Client(timeout=15, headers={"user-agent": "pansol-tracker/1.0"})
        self.clock = clock
        self.price_ttl = price_ttl
        self.no_market_ttl = no_market_ttl
        self.balance_ttl = balance_ttl
        self.outage_cooldown = outage_cooldown
        self._lock = threading.Lock()
        self._coins: dict[str, tuple[float, dict | None]] = {}
        self._balances: dict[str, tuple[float, tuple[float, dict[str, float]]]] = {}
        self._dex_down_until = 0.0

    # ---------- Solana RPC ----------

    def _rpc(self, method: str, params: list) -> dict:
        try:
            r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ChainError(f"{method}: {e}") from e
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ChainError(f"{method}: {body.get('error') if isinstance(body, dict) else 'unexpected reply'}")
        return result

    def balances(self, wallet: str) -> tuple[float, dict[str, float]]:
        """SOL (wrapped SOL included) and every fungible token the wallet holds, by mint."""
        now = self.clock()
        with self._lock:
            hit = self._balances.get(wallet)
        if hit and now - hit[0] < self.balance_ttl:
            return hit[1]

        lamports = _num(self._rpc("getBalance", [wallet, {"commitment": "confirmed"}]).get("value")) or 0.0
        sol = lamports / 1e9
        tokens: dict[str, float] = {}
        for program in TOKEN_PROGRAMS:
            res = self._rpc(
                "getTokenAccountsByOwner",
                [wallet, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            accounts = res.get("value")
            for acc in accounts if isinstance(accounts, list) else []:
                try:
                    info = acc["account"]["data"]["parsed"]["info"]
                    mint = str(info["mint"])
                    amount_info = info["tokenAmount"]
                    decimals = int(amount_info["decimals"])
                    amount = _num(amount_info.get("uiAmountString") or amount_info.get("uiAmount")) or 0.0
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if amount <= 0 or decimals == 0:   # empty accounts and NFTs
                    continue
                if mint == SOL_MINT:
                    sol += amount
                else:
                    tokens[mint] = tokens.get(mint, 0.0) + amount

        result = (sol, tokens)
        with self._lock:
            if len(self._balances) > 5000:
                self._balances.clear()
            self._balances[wallet] = (now, result)
        return result

    # ---------- DexScreener ----------

    def coins(self, mints: Iterable[str], fresh: Iterable[str] = ()) -> tuple[dict[str, dict | None], bool]:
        """Name, price, 24h change, market cap and links per mint (None: no market found).

        The flag is False when DexScreener failed and some mint has neither fresh nor older data.
        Coins without a market are re-checked every `no_market_ttl` seconds, except those in `fresh`
        (watchlist and pinned coins), which may get their first market at any moment.
        """
        now = self.clock()
        fresh = set(fresh)
        out: dict[str, dict | None] = {}
        missing: list[str] = []
        with self._lock:
            dex_down = now < self._dex_down_until
            for m in dict.fromkeys(mints):
                hit = self._coins.get(m)
                ttl = self.price_ttl if hit is None or hit[1] is not None or m in fresh else self.no_market_ttl
                if hit and now - hit[0] < ttl:
                    out[m] = hit[1]
                else:
                    missing.append(m)

        complete = True
        for i in range(0, len(missing), DEXSCREENER_BATCH):
            chunk = missing[i : i + DEXSCREENER_BATCH]
            data = None
            if not dex_down:
                try:
                    r = self.http.get(DEXSCREENER_TOKENS + ",".join(chunk), timeout=DEXSCREENER_TIMEOUT)
                    r.raise_for_status()
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("DexScreener unavailable, pausing for %ss: %s", self.outage_cooldown, e)
                    dex_down = True
                    with self._lock:
                        self._dex_down_until = now + self.outage_cooldown
            if data is None:
                # Keep showing the last known data; never cache the failure.
                with self._lock:
                    for m in missing[i:]:
                        stale = self._coins.get(m)
                        out[m] = stale[1] if stale else None
                        complete = complete and stale is not None
                break

            pairs = data.get("pairs") if isinstance(data, dict) else data
            best: dict[str, tuple[float, dict]] = {}
            wanted = set(chunk)
            for pair in pairs if isinstance(pairs, list) else []:
                if not isinstance(pair, dict):
                    continue
                base = (pair.get("baseToken") or {}).get("address")
                if base not in wanted:
                    continue
                liquidity = _num((pair.get("liquidity") or {}).get("usd")) or 0.0
                if base not in best or liquidity > best[base][0]:
                    best[base] = (liquidity, pair)
            with self._lock:
                if len(self._coins) > 50000:
                    self._coins.clear()
                for m in chunk:
                    coin = _coin(best[m][1]) if m in best else None
                    self._coins[m] = (now, coin)
                    out[m] = coin
        return out, complete

    # ---------- portfolio ----------

    def portfolio(self, wallet: str, watch: Iterable[str] = (), pinned: str | None = None) -> dict:
        sol, tokens = self.balances(wallet)
        mints = list(tokens)
        if len(mints) > MAX_PRICED:
            mints = sorted(mints, key=lambda m: -tokens[m])[:MAX_PRICED]
        watch = list(watch)
        extra = [pinned] if pinned else []
        coins, prices_ok = self.coins([SOL_MINT, *mints, *watch, *extra], fresh=[*watch, *extra])

        holdings = []
        if sol > 0:
            sol_coin = {**(coins.get(SOL_MINT) or _unknown()), "name": "Solana", "symbol": "SOL"}
            holdings.append(_holding(SOL_MINT, sol, sol_coin))
        holdings += [_holding(m, tokens[m], coins.get(m)) for m in mints]
        holdings.sort(key=lambda h: (h["value_usd"] is None, -(h["value_usd"] or 0)))
        total = sum(h["value_usd"] or 0 for h in holdings)
        hidden = len(tokens) + (1 if sol > 0 else 0) - min(len(holdings), MAX_SHOWN)

        return {
            "wallet": wallet,
            "total_usd": total,
            "prices_ok": prices_ok,
            "holdings": holdings[:MAX_SHOWN],
            "coin_count": len(tokens) + (1 if sol > 0 else 0),
            "hidden_tokens": hidden,
            "watchlist": [{"mint": m, **(coins.get(m) or _unknown())} for m in watch],
            "pansol": ({"mint": pinned, **(coins.get(pinned) or _unknown())} if pinned else None),
            "updated_at": int(self.clock()),
        }


def _unknown() -> dict:
    return {"name": "", "symbol": "", "price_usd": None, "change_24h": None, "market_cap": None,
            "liquidity_usd": None, "volume_24h": None, "image": None, "url": None}


def _holding(mint: str, amount: float, coin: dict | None) -> dict:
    c = coin or _unknown()
    price = c["price_usd"]
    return {
        **c,
        "mint": mint,
        "amount": amount,
        "value_usd": amount * price if price is not None else None,
    }
