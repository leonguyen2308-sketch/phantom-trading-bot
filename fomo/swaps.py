"""Turn one Solana transaction into "this wallet bought/sold this token for this many dollars".

Works for any DEX or aggregator (FOMO routes through DFlow; others use Jupiter, Raydium, pump.fun...),
because it reads only the wallet's balance changes, never the program's instructions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {USDC: 6, USDT: 6}
QUOTES = {USDC, USDT, WSOL}

MIN_TRADE_USD = 0.50  # below this it is dust, an airdrop or a fee, not a trade
STABLE_WINS_USD = 0.01  # when a stablecoin moved, small SOL changes are rent and fees, not the price


@dataclass
class Swap:
    sig: str
    slot: int
    t: int  # block time, unix seconds
    wallet: str
    side: str  # "buy" or "sell"
    mint: str
    raw: int  # token amount bought or sold, in base units
    decimals: int
    usd: float  # dollars paid (buy) or received (sell), fees included
    quote: str  # "USDC", "USDT" or "SOL"
    pre_raw: int  # the wallet's balance of this token before the trade
    post_raw: int

    @property
    def amount(self) -> float:
        return self.raw / 10 ** self.decimals

    @property
    def price(self) -> float:
        """USD per whole token."""
        return self.usd / self.amount if self.raw else 0.0

    @property
    def sold_fraction(self) -> float:
        """For a sell: the share of the wallet's holding that was sold (1.0 = all of it)."""
        if self.side != "sell" or self.pre_raw <= 0:
            return 0.0
        return min(1.0, self.raw / self.pre_raw)

    def to_dict(self) -> dict:
        return asdict(self)


def _keys(tx: dict) -> list[str]:
    keys = tx["transaction"]["message"]["accountKeys"]
    out = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    if len(out) < len((tx["meta"].get("preBalances") or [])):  # raw encoding: lookup-table keys come after
        out += list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
    return out


def _token_totals(entries: list[dict], wallet: str) -> tuple[dict[str, int], dict[str, int]]:
    totals: dict[str, int] = {}
    decimals: dict[str, int] = {}
    for e in entries or []:
        if e.get("owner") != wallet:
            continue
        amt = e.get("uiTokenAmount") or {}
        totals[e["mint"]] = totals.get(e["mint"], 0) + int(amt.get("amount", 0))
        decimals[e["mint"]] = int(amt.get("decimals", 0))
    return totals, decimals


def parse_swap(tx: dict | None, wallet: str, sol_usd: float) -> Swap | None:
    """The wallet's trade in this transaction, or None if it wasn't a clean one-token buy or sell."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta = tx["meta"]
    pre, dec_pre = _token_totals(meta.get("preTokenBalances"), wallet)
    post, dec_post = _token_totals(meta.get("postTokenBalances"), wallet)
    decimals = {**dec_pre, **dec_post}
    delta = {m: post.get(m, 0) - pre.get(m, 0) for m in set(pre) | set(post)}

    # Native SOL change of the wallet itself; the network fee doesn't count as trading.
    sol_lamports = 0
    keys = _keys(tx)
    if wallet in keys:
        i = keys.index(wallet)
        sol_lamports = int(meta["postBalances"][i]) - int(meta["preBalances"][i])
        if i == 0:
            sol_lamports += int(meta.get("fee", 0))
    sol_lamports += delta.pop(WSOL, 0)

    stable = {m: delta.pop(m, 0) / 10 ** d for m, d in STABLES.items()}
    stable_usd = sum(stable.values())
    tokens = {m: d for m, d in delta.items() if d != 0}
    if len(tokens) != 1:
        return None  # nothing traded, or a token-for-token swap we don't copy
    mint, d = next(iter(tokens.items()))

    if abs(stable_usd) >= STABLE_WINS_USD:
        quote_usd, quote = stable_usd, ("USDT" if abs(stable[USDT]) > abs(stable[USDC]) else "USDC")
    else:
        quote_usd, quote = sol_lamports / 1e9 * sol_usd, "SOL"

    if d > 0 and quote_usd <= -MIN_TRADE_USD:
        side, usd = "buy", -quote_usd
    elif d < 0 and quote_usd >= MIN_TRADE_USD:
        side, usd = "sell", quote_usd
    else:
        return None  # a transfer, airdrop or dust
    sig = (tx["transaction"].get("signatures") or [""])[0]
    return Swap(sig=sig, slot=int(tx.get("slot", 0)), t=int(tx.get("blockTime") or 0), wallet=wallet, side=side, mint=mint,
                raw=abs(d), decimals=decimals.get(mint, 0), usd=round(usd, 6), quote=quote,
                pre_raw=pre.get(mint, 0), post_raw=post.get(mint, 0))
