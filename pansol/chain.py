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
DEXSCREENER_BATCH = 30      # addresses per request, the API's limit
MAX_TOKENS = 300            # coins priced per wallet; wallets full of airdropped spam stop here


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
        balance_ttl: float = 20,
    ):
        self.rpc_url = rpc_url
        self.http = http or httpx.Client(timeout=15, headers={"user-agent": "pansol-tracker/1.0"})
        self.clock = clock
        self.price_ttl = price_ttl
        self.balance_ttl = balance_ttl
        self._lock = threading.Lock()
        self._coins: dict[str, tuple[float, dict | None]] = {}
        self._balances: dict[str, tuple[float, tuple[float, dict[str, float]]]] = {}

    # ---------- Solana RPC ----------

    def _rpc(self, method: str, params: list):
        try:
            r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ChainError(f"{method}: {e}") from e
        if not isinstance(body, dict) or "error" in body or "result" not in body:
            raise ChainError(f"{method}: {body.get('error') if isinstance(body, dict) else body}")
        return body["result"]

    def balances(self, wallet: str) -> tuple[float, dict[str, float]]:
        """SOL (wrapped SOL included) and every fungible token the wallet holds, by mint."""
        now = self.clock()
        with self._lock:
            hit = self._balances.get(wallet)
        if hit and now - hit[0] < self.balance_ttl:
            return hit[1]

        sol = (self._rpc("getBalance", [wallet, {"commitment": "confirmed"}]).get("value") or 0) / 1e9
        tokens: dict[str, float] = {}
        for program in TOKEN_PROGRAMS:
            res = self._rpc(
                "getTokenAccountsByOwner",
                [wallet, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            for acc in res.get("value") or []:
                try:
                    info = acc["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    amount_info = info["tokenAmount"]
                    decimals = int(amount_info["decimals"])
                    amount = _num(amount_info.get("uiAmountString") or amount_info.get("uiAmount")) or 0.0
                except (KeyError, TypeError, ValueError):
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

    def coins(self, mints: Iterable[str]) -> dict[str, dict | None]:
        """Name, price, 24h change, market cap and links per mint. None when no market exists."""
        now = self.clock()
        out: dict[str, dict | None] = {}
        missing: list[str] = []
        with self._lock:
            for m in dict.fromkeys(mints):
                hit = self._coins.get(m)
                if hit and now - hit[0] < self.price_ttl:
                    out[m] = hit[1]
                else:
                    missing.append(m)

        for i in range(0, len(missing), DEXSCREENER_BATCH):
            chunk = missing[i : i + DEXSCREENER_BATCH]
            try:
                r = self.http.get(DEXSCREENER_TOKENS + ",".join(chunk))
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("DexScreener unavailable: %s", e)
                for m in chunk:
                    out.setdefault(m, None)      # show the coin without a price; retry next time
                continue
            pairs = data.get("pairs") if isinstance(data, dict) else data
            best: dict[str, tuple[float, dict]] = {}
            wanted = set(chunk)
            for pair in pairs or []:
                if not isinstance(pair, dict):
                    continue
                base = (pair.get("baseToken") or {}).get("address")
                if base not in wanted:
                    continue
                liquidity = _num((pair.get("liquidity") or {}).get("usd")) or 0.0
                if base not in best or liquidity > best[base][0]:
                    best[base] = (liquidity, pair)
            with self._lock:
                if len(self._coins) > 20000:
                    self._coins.clear()
                for m in chunk:
                    coin = _coin(best[m][1]) if m in best else None
                    self._coins[m] = (now, coin)
                    out[m] = coin
        return out

    # ---------- portfolio ----------

    def portfolio(self, wallet: str, watch: Iterable[str] = (), pinned: str | None = None) -> dict:
        sol, tokens = self.balances(wallet)
        mints = list(tokens)
        hidden = max(0, len(mints) - MAX_TOKENS)
        if hidden:
            mints = sorted(mints, key=lambda m: -tokens[m])[:MAX_TOKENS]
        watch = list(watch)
        extra = [pinned] if pinned else []
        coins = self.coins([SOL_MINT, *mints, *watch, *extra])

        holdings = []
        if sol > 0:
            sol_coin = {**(coins.get(SOL_MINT) or _unknown()), "name": "Solana", "symbol": "SOL"}
            holdings.append(_holding(SOL_MINT, sol, sol_coin))
        holdings += [_holding(m, tokens[m], coins.get(m)) for m in mints]
        holdings.sort(key=lambda h: (h["value_usd"] is None, -(h["value_usd"] or 0)))
        total = sum(h["value_usd"] or 0 for h in holdings)

        return {
            "wallet": wallet,
            "total_usd": total,
            "holdings": holdings,
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
