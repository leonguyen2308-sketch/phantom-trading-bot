"""Buying and selling memecoins for USDC through Jupiter.

PaperBroker: real Jupiter quotes for real tokens, fake money (a JSON file), plus extra slippage for the
seconds a live copy takes to land. LiveBroker: the same quotes, signed by the bot wallet and sent.
Every live transaction goes through the same inspection as the BTC bot (no approvals, no authority
changes, no SOL drained) before it is signed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from solders.keypair import Keypair

from fomo.chain import SolanaRpc
from fomo.swaps import USDC
from phantom.execution.jupiter import DEFAULT_BASE, POLL_SECONDS, check_order, impact_bps, submit_order


@dataclass
class Quote:
    side: str  # "buy" (USDC -> token) or "sell" (token -> USDC)
    mint: str
    usd: float  # USDC paid (buy) or expected USDC received (sell)
    raw: int  # tokens received (buy) or sold (sell), base units
    min_out: int  # the worst output the transaction accepts, base units
    impact_pct: float
    payload: Any = None


@dataclass
class Fill:
    side: str
    mint: str
    raw: int
    usd: float
    ref: str


def jupiter_order(http: httpx.Client, base: str, headers: dict, input_mint: str, output_mint: str, amount: int,
                  taker: str | None = None) -> dict:
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount)}
    if taker:
        params["taker"] = taker
    r = http.get(f"{base}/order", params=params, headers=headers)
    if r.status_code == 400:
        try:
            detail = r.json().get("error") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        raise ValueError(f"no route: {detail}")
    r.raise_for_status()
    o = r.json()
    if o.get("inputMint") != input_mint or o.get("outputMint") != output_mint or int(o.get("inAmount", -1)) != amount:
        raise ValueError("Jupiter quote does not match the request")
    if int(o.get("outAmount", 0)) <= 0:
        raise ValueError(f"Jupiter has no route: {o.get('error') or o.get('errorMessage') or 'no output'}")
    return o


def _quote(o: dict, side: str, mint: str) -> Quote:
    if side == "buy":
        usd, raw = int(o["inAmount"]) / 1e6, int(o["outAmount"])
    else:
        usd, raw = int(o["outAmount"]) / 1e6, int(o["inAmount"])
    return Quote(side, mint, usd, raw, int(o.get("otherAmountThreshold") or 0), impact_bps(o) / 100, o)


def _delta(tx: dict, owner: str, mint: str) -> int:
    """Change in `owner`'s balance of `mint` (base units) made by one jsonParsed transaction."""
    meta = tx.get("meta") or {}

    def total(key):
        return sum(int(((b.get("uiTokenAmount") or {}).get("amount")) or 0) for b in meta.get(key) or []
                   if b.get("owner") == owner and b.get("mint") == mint)
    return total("postTokenBalances") - total("preTokenBalances")


class PaperBroker:
    mode = "paper"

    def __init__(self, path: str | Path, api_key: str, start_usd: float = 100.0, slippage_bps: float = 100.0,
                 base: str = DEFAULT_BASE, http: httpx.Client | None = None):
        self.path = Path(path)
        self.http, self.base, self.headers = http or httpx.Client(timeout=20), base.rstrip("/"), {"x-api-key": api_key}
        self.slippage = slippage_bps / 1e4
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {"usdc": float(start_usd), "tokens": {}, "start": float(start_usd)}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    def usdc(self) -> float:
        return float(self.state["usdc"])

    def sol(self) -> float:
        return 1.0  # paper trades pay no network fees

    def token_raw(self, mint: str) -> int:
        return int(self.state["tokens"].get(mint, 0))

    def holdings(self) -> dict[str, int]:
        return {m: int(v) for m, v in self.state["tokens"].items() if int(v) > 0}

    def quote_buy(self, mint: str, usd: float) -> Quote:
        return _quote(jupiter_order(self.http, self.base, self.headers, USDC, mint, int(usd * 1e6)), "buy", mint)

    def quote_sell(self, mint: str, raw: int) -> Quote:
        return _quote(jupiter_order(self.http, self.base, self.headers, mint, USDC, int(raw)), "sell", mint)

    def execute(self, q: Quote) -> Fill:
        if q.side == "buy":
            if q.usd > self.usdc() + 1e-9:
                raise RuntimeError("not enough paper USDC")
            got = int(q.raw * (1 - self.slippage))
            self.state["usdc"] = self.usdc() - q.usd
            self.state["tokens"][q.mint] = self.token_raw(q.mint) + got
            self._save()
            return Fill("buy", q.mint, got, q.usd, f"paper-{int(time.time() * 1000)}")
        raw = min(q.raw, self.token_raw(q.mint))
        usd = q.usd * (raw / q.raw if q.raw else 0) * (1 - self.slippage)
        self.state["usdc"] = self.usdc() + usd
        self.state["tokens"][q.mint] = self.token_raw(q.mint) - raw
        if self.state["tokens"][q.mint] <= 0:
            del self.state["tokens"][q.mint]
        self._save()
        return Fill("sell", q.mint, raw, usd, f"paper-{int(time.time() * 1000)}")


class LiveBroker:
    mode = "live"

    def __init__(self, kp: Keypair, api_key: str, rpc: SolanaRpc, base: str = DEFAULT_BASE, http: httpx.Client | None = None,
                 poll_seconds: float = POLL_SECONDS, sleep=time.sleep):
        self.kp, self.rpc = kp, rpc
        self.http, self.base, self.headers = http or httpx.Client(timeout=20), base.rstrip("/"), {"x-api-key": api_key}
        self.poll_seconds, self.sleep = poll_seconds, sleep
        self.address = str(kp.pubkey())

    def holdings(self) -> dict[str, int]:
        return {m: v for m, v in self.rpc.token_balances(self.address).items() if v > 0}

    def usdc(self) -> float:
        return self.holdings().get(USDC, 0) / 1e6

    def sol(self) -> float:
        return self.rpc.sol_balance(self.address)

    def token_raw(self, mint: str) -> int:
        return self.holdings().get(mint, 0)

    def _order(self, input_mint: str, output_mint: str, amount: int) -> dict:
        o = jupiter_order(self.http, self.base, self.headers, input_mint, output_mint, amount, taker=self.address)
        check_order(o, input_mint=input_mint, output_mint=output_mint, amount=amount)
        return o

    def quote_buy(self, mint: str, usd: float) -> Quote:
        return _quote(self._order(USDC, mint, int(usd * 1e6)), "buy", mint)

    def quote_sell(self, mint: str, raw: int) -> Quote:
        return _quote(self._order(mint, USDC, int(raw)), "sell", mint)

    def settle(self, mint: str, side: str, since: float) -> Fill | None:
        """What a swap sent at `since` (unix seconds) really did, read from the wallet's own transactions: the fill, or
        None when no transaction since then moved this coin that way. Each transaction is read on its own, so swaps of
        other coins landing at the same time don't mix in. Raises when the chain can't be read (try again later)."""
        for s in self.rpc.signatures(self.address, limit=25):  # newest first
            bt = s.get("blockTime")
            if bt is not None and bt < since - 5:
                break
            if s.get("err") is not None:
                continue
            tx = self.rpc.transaction(s["signature"])
            if not tx:
                continue
            d_tok, d_usd = _delta(tx, self.address, mint), _delta(tx, self.address, USDC)
            if side == "buy" and d_tok > 0 and d_usd < 0:
                return Fill("buy", mint, d_tok, -d_usd / 1e6, s["signature"])
            if side == "sell" and d_tok < 0 and d_usd > 0:
                return Fill("sell", mint, -d_tok, d_usd / 1e6, s["signature"])
        return None

    def execute(self, q: Quote) -> Fill:
        res = submit_order(self.http, self.base, self.headers, self.kp, q.payload, self.poll_seconds, self.sleep)
        o, sig = q.payload, res.get("signature", "")
        out_raw = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or o["outAmount"])
        in_raw = int(res.get("totalInputAmount") or res.get("inputAmountResult") or o["inAmount"])
        if q.side == "buy":
            return Fill("buy", q.mint, out_raw, in_raw / 1e6, sig)
        return Fill("sell", q.mint, in_raw, out_raw / 1e6, sig)
