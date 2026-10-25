"""A simulated FOMO world for `./fomo.sh demo`: six made-up traders, made-up memecoins, fake money.

Nothing here touches the internet. It runs the real bot code (rules, sizing, safety checks, exits, the
fire rule, the dashboard) against invented trades so you can watch it work. The numbers mean nothing
about real results. It does model what makes copying hard: other copiers pump the price right after
the trader buys, and dump it right after the trader sells, and our copy lands a few seconds late.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from solders.keypair import Keypair

from fomo.brokers import Fill, Quote
from fomo.chain import MintInfo
from fomo.leaderboard import Trader
from fomo.market import TokenStats
from fomo.swaps import Swap

STEP = 30  # seconds between price points


@dataclass
class SimToken:
    mint: str
    symbol: str
    t0: float
    path: list[float]
    liquidity: float
    mcap: float
    freeze: bool
    creator: str = ""
    jumps: list[tuple[float, float]] = field(default_factory=list)  # (time, multiplier)

    def price(self, t: float) -> float:
        i = (t - self.t0) / STEP
        if i <= 0:
            p = self.path[0]
        elif i >= len(self.path) - 1:
            p = self.path[-1]
        else:
            j = int(i)
            p = self.path[j] + (self.path[j + 1] - self.path[j]) * (i - j)
        for jt, m in self.jumps:
            if t >= jt:
                p *= m
        return max(p, 1e-12)


@dataclass
class SimTrader:
    trader: Trader
    edge: float  # drift of the coins they pick: >0 good picker
    every: float  # average seconds between buys
    next_buy: float = 0.0
    holdings: dict[str, int] = field(default_factory=dict)


SYLLABLES = ["MO", "OG", "PEP", "WIF", "BON", "ZO", "KAI", "NYA", "TOAD", "CHI", "RP", "FLUF", "GOB", "LAST", "MOCH", "SNEK", "BLOB", "ZOOM"]


class SimWorld:
    def __init__(self, seed: int | None = None, speed: float = 60.0, latency: float = 6.0, n_traders: int = 6, start: float | None = None):
        self.rng = random.Random(seed)
        self.speed, self.latency = speed, latency
        self.real0 = time.monotonic()
        self.t0 = start or time.time()  # the Grok demo starts a minute before its night window opens
        self.tokens: dict[str, SimToken] = {}
        self.events: list[Swap] = []
        names = [("sim_ace", 0.55, 900), ("sim_blaze", 0.35, 700), ("sim_cobra", 0.25, 800),
                 ("sim_delta", 0.1, 600), ("sim_echo", 0.0, 1000), ("sim_fox", -0.1, 750)]
        if n_traders != 6:  # a crowd: most are no better than chance, a few are good, a few are bad
            names = [(f"sim_{i:03d}", self.rng.gauss(0.05, 0.25), self.rng.uniform(1500, 6000)) for i in range(1, n_traders + 1)]
        self.rng.shuffle(names)  # the leaderboard order doesn't reveal who is actually good
        self.traders: list[SimTrader] = []
        for i, (h, edge, every) in enumerate(names, 1):
            tr = Trader(handle=h, wallet=str(Keypair().pubkey()), rank=i, pnl_7d=round(self.rng.uniform(8_000, 60_000) / i, 0),
                        win_rate=round(self.rng.uniform(0.55, 0.75), 2), source="simulation")
            self.traders.append(SimTrader(tr, edge, every, next_buy=self.t0 + self.rng.uniform(30, every)))

    # ---- time ----
    def clock(self) -> float:
        return self.t0 + (time.monotonic() - self.real0) * self.speed

    def lineup(self) -> list[Trader]:
        return [s.trader for s in self.traders]

    # ---- the world moves ----
    def _new_token(self, t: float, edge: float) -> SimToken:
        n = int(6 * 3600 / STEP)
        vol = self.rng.uniform(0.02, 0.05)
        drift = edge * vol * 0.12 + self.rng.gauss(0, vol * 0.08)
        path, p = [], self.rng.uniform(0.00001, 0.01)
        for _ in range(n):
            path.append(p)
            p *= math.exp(drift + self.rng.gauss(0, vol))
            if self.rng.random() < 0.001:  # rug
                p *= 0.05
        sym = self.rng.choice(SYLLABLES) + self.rng.choice(["", "AI", "INU", "CAT", "2"])
        liq = self.rng.lognormvariate(math.log(60_000), 0.9)
        tok = SimToken(mint=str(Keypair().pubkey()), symbol=sym, t0=t, path=path, liquidity=liq,
                       mcap=liq * self.rng.uniform(3, 12), freeze=self.rng.random() < 0.06,
                       creator=str(Keypair().pubkey()))
        self.tokens[tok.mint] = tok
        return tok

    def advance(self) -> None:
        now = self.clock()
        for st in self.traders:
            while st.next_buy <= now:
                t = st.next_buy
                st.next_buy += self.rng.expovariate(1 / st.every)
                tok = self._new_token(t, st.edge)
                usd = round(self.rng.lognormvariate(math.log(600), 0.7), 2)
                px = tok.price(t)
                raw = int(usd / px * 1e6)
                self.events.append(Swap(sig=f"sim-{len(self.events)}", slot=len(self.events), t=int(t), wallet=st.trader.wallet,
                                        side="buy", mint=tok.mint, raw=raw, decimals=6, usd=usd, quote="USDC", pre_raw=0, post_raw=raw))
                tok.jumps.append((t + 3, 1 + self.rng.uniform(0.03, 0.15)))  # other copiers pile in
                hold = self.rng.lognormvariate(math.log(40 * 60), 0.8)
                parts = [0.5, 1.0] if self.rng.random() < 0.3 else [1.0]
                left = raw
                for k, share in enumerate(parts):
                    ts = t + hold * (1 + k * 0.5)
                    sell_raw = left if share == 1.0 else int(raw * share)
                    self.events.append(Swap(sig=f"sim-{len(self.events)}", slot=len(self.events), t=int(ts), wallet=st.trader.wallet,
                                            side="sell", mint=tok.mint, raw=sell_raw, decimals=6, usd=0.0, quote="USDC",
                                            pre_raw=left, post_raw=left - sell_raw))
                    left -= sell_raw
                    tok.jumps.append((ts + 3, 1 - self.rng.uniform(0.04, 0.18)))  # and pile out
        self.events.sort(key=lambda e: e.t)


class SimWatcher:
    ws_connected = True

    def __init__(self, world: SimWorld):
        self.w = world
        self.wallets: set[str] = set()
        self.priority: set[str] = set()

    def status(self) -> str:
        return f"simulated: {len(self.wallets)} wallets"

    def watch(self, wallets: set[str]) -> None:
        self.wallets = set(wallets)

    def poll(self) -> list[Swap]:
        self.w.advance()
        now = self.w.clock()
        out, keep = [], []
        for e in self.w.events:
            if e.t + self.w.latency > now:
                keep.append(e)
                continue
            if e.wallet in self.wallets and now - e.t < 600:
                if e.side == "sell":
                    e.usd = round(e.raw / 1e6 * self.w.tokens[e.mint].price(e.t), 2)
                out.append(e)
        self.w.events = keep
        return out


class SimPrices:
    def __init__(self, world: SimWorld):
        self.w = world

    def get(self, mints: list[str]) -> dict[str, float]:
        now = self.w.clock()
        return {m: self.w.tokens[m].price(now) for m in mints if m in self.w.tokens}


class SimDex:
    def __init__(self, world: SimWorld):
        self.w = world

    def get(self, mint: str) -> TokenStats | None:
        tok = self.w.tokens.get(mint)
        if not tok:
            return None
        return TokenStats(mint, tok.symbol, tok.price(self.w.clock()), tok.liquidity, tok.mcap, int(tok.t0))

    def mint_info(self, mint: str) -> MintInfo:
        tok = self.w.tokens[mint]
        return MintInfo(mint, 6, None, tok.creator if tok.freeze else None, "spl-token")


class SimBroker:
    mode = "demo"

    def __init__(self, world: SimWorld, start_usd: float = 100.0, slippage_bps: float = 100.0):
        self.w = world
        self.cash = start_usd
        self.tokens: dict[str, int] = {}
        self.slippage = slippage_bps / 1e4

    def usdc(self) -> float:
        return self.cash

    def sol(self) -> float:
        return 1.0

    def token_raw(self, mint: str) -> int:
        return self.tokens.get(mint, 0)

    def holdings(self) -> dict[str, int]:
        return dict(self.tokens)

    def _impact(self, mint: str, usd: float) -> float:
        return usd / self.w.tokens[mint].liquidity

    def quote_buy(self, mint: str, usd: float) -> Quote:
        px = self.w.tokens[mint].price(self.w.clock())
        imp = self._impact(mint, usd)
        raw = int(usd / px * (1 - imp) * 1e6)
        return Quote("buy", mint, usd, raw, int(raw * 0.9), imp * 100)

    def quote_sell(self, mint: str, raw: int) -> Quote:
        px = self.w.tokens[mint].price(self.w.clock())
        usd = raw / 1e6 * px
        imp = self._impact(mint, usd)
        return Quote("sell", mint, usd * (1 - imp), raw, int(usd * (1 - imp) * 0.9 * 1e6), imp * 100)

    def execute(self, q: Quote) -> Fill:
        if q.side == "buy":
            got = int(q.raw * (1 - self.slippage))
            self.cash -= q.usd
            self.tokens[q.mint] = self.tokens.get(q.mint, 0) + got
            return Fill("buy", q.mint, got, q.usd, "sim")
        raw = min(q.raw, self.tokens.get(q.mint, 0))
        usd = q.usd * (raw / q.raw if q.raw else 0) * (1 - self.slippage)
        self.cash += usd
        self.tokens[q.mint] = self.tokens.get(q.mint, 0) - raw
        return Fill("sell", q.mint, raw, usd, "sim")
