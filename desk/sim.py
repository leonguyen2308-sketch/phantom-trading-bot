"""A simulated memecoin market for `./desk.sh demo`: made-up tokens, a rule-based stand-in for the Claude desk,
fake money. No internet, no keys. It runs the real desk code (funnel, plan, sizing, limits, shadow book,
dashboard) so you can watch it work. The numbers say nothing about real results. Like the real market, most
simulated tokens fail the gates, many rug, and the ones that pass still lose more often than they win.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from solders.keypair import Keypair

from desk.ai import Opinion, DeskUnavailable
from fomo.brokers import Fill, Quote
from fomo.chain import MintInfo

NAMES = ["PEPE", "WIF", "BONK", "MOO", "GIGA", "NYAN", "TOAD", "CHAD", "MOCHI", "FROG", "KITTY", "DOGE", "SNEK", "BLOB", "ZOOM", "LUX"]


@dataclass
class SimToken:
    mint: str
    symbol: str
    born: float
    price: float
    liquidity: float
    supply: float
    drift: float  # per-minute log drift; changes over the token's life
    vol: float
    rug_at: float | None
    mint_auth: bool
    freeze_auth: bool
    top1: float
    top10: float
    fresh: float
    organic: float
    sus: bool
    history: list[tuple[float, float, float]] = field(default_factory=list)  # (t, price, volume_usd per minute)
    dead: bool = False


class SimMarket:
    def __init__(self, seed: int | None = None, start: float | None = None, minutes_per_step: float = 0.5):
        self.rng = random.Random(seed)
        self.t = start or datetime.now(timezone.utc).timestamp()
        self.step_s = minutes_per_step * 60
        self.tokens: dict[str, SimToken] = {}
        for _ in range(40):
            self._spawn(age_min=self.rng.uniform(5, 600))

    def clock(self) -> float:
        return self.t

    def _spawn(self, age_min: float = 0.0) -> None:
        r = self.rng
        kind = r.random()
        # most launches are junk: authority left on, concentrated, fresh-wallet farms or outright rugs
        tok = SimToken(mint=str(Keypair().pubkey()), symbol=r.choice(NAMES) + r.choice(["", "AI", "INU", "CAT", "2"]),
                       born=self.t - age_min * 60, price=r.uniform(1e-5, 5e-3), liquidity=r.lognormvariate(math.log(40_000), 1.0),
                       supply=1e9, drift=r.gauss(0.0005 if kind < 0.25 else -0.0015, 0.004), vol=r.uniform(0.01, 0.04),
                       rug_at=self.t + r.uniform(10, 400) * 60 if kind > 0.55 else None,
                       mint_auth=r.random() < 0.15, freeze_auth=r.random() < 0.12, top1=r.uniform(1, 12), top10=r.uniform(8, 40),
                       fresh=r.choice([0, 0, 0, 2, 5, 15]), organic=r.uniform(5, 95), sus=r.random() < 0.1)
        for k in range(int(min(age_min, 360))):
            tok.history.append((tok.born + k * 60, tok.price, tok.liquidity * r.uniform(0.01, 0.2)))
        self.tokens[tok.mint] = tok

    def advance(self) -> None:
        self.t += self.step_s
        r = self.rng
        if r.random() < 0.5:
            self._spawn()
        for tok in list(self.tokens.values()):
            if tok.dead:
                continue
            for _ in range(max(1, int(self.step_s // 60))):
                if r.random() < 0.01:
                    tok.drift = r.gauss(-0.001, 0.004)  # regime change
                tok.price *= math.exp(tok.drift + r.gauss(0, tok.vol))
                vol_usd = tok.liquidity * r.uniform(0.01, 0.2) * (1 + max(tok.drift, 0) * 300)
                tok.history.append((self.t, tok.price, vol_usd))
            tok.history = tok.history[-720:]
            if tok.rug_at and self.t >= tok.rug_at:
                tok.price *= 0.03
                tok.liquidity *= 0.02
                tok.dead = True
        for m in [m for m, tok in self.tokens.items() if self.t - tok.born > 4 * 86400]:
            del self.tokens[m]

    # ---- numbers in the shapes the real sources return ----
    def _window(self, tok: SimToken, minutes: float) -> list[tuple[float, float, float]]:
        return [h for h in tok.history if h[0] > self.t - minutes * 60]

    def _change(self, tok: SimToken, minutes: float) -> float | None:
        w = self._window(tok, minutes)
        return (tok.price / w[0][1] - 1) * 100 if w and w[0][1] else None

    def jup_record(self, tok: SimToken) -> dict:
        w1 = self._window(tok, 60)
        vol1 = sum(h[2] for h in w1)
        mcap = tok.price * tok.supply
        return {"id": tok.mint, "symbol": tok.symbol, "mcap": mcap, "liquidity": tok.liquidity, "holderCount": int(200 + tok.liquidity / 50),
                "organicScore": tok.organic, "firstPool": {"id": "pool" + tok.mint[:8], "createdAt": datetime.fromtimestamp(tok.born, timezone.utc).isoformat()},
                "stats1h": {"holderChange": self.rng.uniform(-5, 15), "buyVolume": vol1 / 2, "buyOrganicVolume": vol1 / 2 * tok.organic / 100,
                            "numTraders": int(vol1 / 300)},
                "audit": {"isSus": tok.sus, "mintAuthorityDisabled": not tok.mint_auth, "freezeAuthorityDisabled": not tok.freeze_auth,
                          "topHoldersPercentage": tok.top10, "devBalancePercentage": self.rng.choice([0, 0, 1, 3, 12])}}

    def dex_pair(self, tok: SimToken) -> dict:
        w5, w1 = self._window(tok, 5), self._window(tok, 60)
        v5, v1, v6 = sum(h[2] for h in w5), sum(h[2] for h in w1), sum(h[2] for h in self._window(tok, 360))
        up = max(tok.drift, -0.002) * 400
        b1 = int(v1 / 120 * (1 + up / 4))
        s1 = int(v1 / 120 * (1 - up / 4)) or 1
        return {"chainId": "solana", "dexId": "pumpswap", "pairAddress": "pool" + tok.mint[:8], "baseToken": {"address": tok.mint, "symbol": tok.symbol},
                "priceUsd": tok.price, "liquidity": {"usd": tok.liquidity}, "_total_liquidity_usd": tok.liquidity, "marketCap": tok.price * tok.supply,
                "pairCreatedAt": tok.born * 1000, "volume": {"m5": v5, "h1": v1, "h6": v6},
                "txns": {"m5": {"buys": max(b1 // 12, 0), "sells": max(s1 // 12, 0)}, "h1": {"buys": b1, "sells": s1}},
                "priceChange": {"m5": self._change(tok, 5), "h1": self._change(tok, 60), "h6": self._change(tok, 360), "h24": self._change(tok, 1440)},
                "boosts": {"active": 1 if self.rng.random() < 0.1 else 0}, "info": {"websites": [{"url": "https://example.org"}], "socials": []}}

    def candles(self, tok: SimToken, minutes: int = 5) -> list[list[float]]:
        out: dict[int, list[float]] = {}
        for t, p, v in tok.history:
            b = int(t // (minutes * 60)) * minutes * 60
            c = out.get(b)
            if c is None:
                out[b] = [b, p, p, p, p, v]
            else:
                c[2], c[3], c[4], c[5] = max(c[2], p), min(c[3], p), p, c[5] + v
        return [out[k] for k in sorted(out)]


class SimJup:
    def __init__(self, m: SimMarket):
        self.m = m

    def top(self, kind="toptrending", window="5m", limit=100):
        live = [t for t in self.m.tokens.values() if not t.dead]
        live.sort(key=lambda t: sum(h[2] for h in self.m._window(t, 5)), reverse=True)
        return [self.m.jup_record(t) for t in live[:limit]]

    def recent(self):
        live = sorted((t for t in self.m.tokens.values() if not t.dead), key=lambda t: -t.born)
        return [self.m.jup_record(t) for t in live[:30]]

    def search(self, mints):
        return {m: self.m.jup_record(self.m.tokens[m]) for m in mints if m in self.m.tokens}


class SimDex:
    def __init__(self, m: SimMarket):
        self.m = m

    def tokens(self, mints):
        return {x: self.m.dex_pair(self.m.tokens[x]) for x in mints if x in self.m.tokens}

    def discovered(self):
        return []


class SimGecko:
    def __init__(self, m: SimMarket):
        self.m = m

    def candles(self, pool, minutes=5, limit=72, token=None):
        tok = next((t for t in self.m.tokens.values() if "pool" + t.mint[:8] == pool), None)
        return self.m.candles(tok, minutes)[-limit:] if tok else []


class SimRug:
    def __init__(self, m: SimMarket):
        self.m = m

    def summary(self, mint):
        tok = self.m.tokens.get(mint)
        if not tok:
            return None
        return {"score_normalised": 70 if tok.rug_at else self.m.rng.uniform(5, 40), "risks": [], "lpLockedPct": 100}


class SimHolders:
    """Top holders and fresh-wallet share of the token most recently asked about."""

    def __init__(self, m: SimMarket):
        self.m = m
        self._last = ""

    def top(self, mint, pools=()):
        self._last = mint
        tok = self.m.tokens[mint]
        rest = max(tok.top10 - tok.top1, 0) / 9
        return [("w%d" % i, tok.top1 if i == 0 else rest) for i in range(10)]

    def fresh_share(self, holders, hours, check=10):
        tok = self.m.tokens.get(self._last)
        return (tok.fresh if tok else 0.0), min(len(holders), check)


class SimPrices:
    def __init__(self, m: SimMarket):
        self.m = m

    def get(self, mints):
        return {x: self.m.tokens[x].price for x in mints if x in self.m.tokens}


def sim_mint_info(m: SimMarket):
    def info(mint):
        tok = m.tokens[mint]
        return MintInfo(mint, 6, "A" * 44 if tok.mint_auth else None, "F" * 44 if tok.freeze_auth else None, "spl-token")
    return info


class SimBroker:
    mode = "demo"

    def __init__(self, m: SimMarket, start_usd: float = 100.0, slippage_bps: float = 150.0):
        self.m, self.cash, self.tok, self.slip = m, start_usd, {}, slippage_bps / 1e4

    def usdc(self):
        return self.cash

    def sol(self):
        return 1.0

    def holdings(self):
        return dict(self.tok)

    def token_raw(self, mint):
        return self.tok.get(mint, 0)

    def _impact(self, mint, usd):
        return usd / max(self.m.tokens[mint].liquidity, 1) * 100

    def quote_buy(self, mint, usd):
        tok = self.m.tokens[mint]
        if tok.dead:
            raise ValueError("no route")
        imp = self._impact(mint, usd)
        raw = int(usd / tok.price * (1 - imp / 100) * 1e6)
        return Quote("buy", mint, usd, raw, int(raw * 0.99), imp)

    def quote_sell(self, mint, raw):
        tok = self.m.tokens[mint]
        usd = raw / 1e6 * tok.price
        imp = self._impact(mint, usd)
        out = usd * (1 - imp / 100)
        return Quote("sell", mint, out, raw, int(out * 0.99 * 1e6), imp)

    def execute(self, q):
        if q.side == "buy":
            got = int(q.raw * (1 - self.slip))
            self.cash -= q.usd
            self.tok[q.mint] = self.tok.get(q.mint, 0) + got
            return Fill("buy", q.mint, got, q.usd, "sim")
        raw = min(q.raw, self.tok.get(q.mint, 0))
        usd = q.usd * (raw / q.raw if q.raw else 0) * (1 - self.slip)
        self.cash += usd
        self.tok[q.mint] = self.tok.get(q.mint, 0) - raw
        return Fill("sell", q.mint, raw, usd, "sim")


class RuleDesk:
    """Stands in for the Claude seats in the demo: reads the same numbers, answers in the same shape."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.spent = 0.0

    def spent_today(self) -> float:
        return self.spent

    def review(self, legend, s, plan, symbol="", mint="", links=None) -> Opinion:
        if self.rng.random() < 0.03:
            raise DeskUnavailable("simulated API hiccup")
        self.spent += 0.07
        accel, bs = s.get("vol_accel") or 0, s.get("buy_sell_ratio_1h") or 0
        drop, r30 = s.get("drop_from_high_pct") or 0, s.get("return_30m_pct") or 0
        if (s.get("organic_score") or 0) < 40:
            setup = "wash_or_bot"
        elif drop > 15:
            setup = "fading"
        elif r30 > 30:
            setup = "late_chase"
        elif accel > 1.2 and bs > 1.05:
            setup = "early_breakout"
        elif bs > 1.0:
            setup = "organic_trend"
        else:
            setup = "unclear"
        window = "early" if r30 < 10 and drop < 8 else "running" if drop < 12 else "late"
        conv = max(0.0, min(1.0, 0.6 + 0.1 * (accel - 1) + 0.3 * (bs - 1) - drop / 100 + self.rng.gauss(0, 0.08)))
        top10 = s.get("top10_pct") or 0
        verdict = "block" if top10 > 25 or (s.get("fresh_share_pct") or 0) > 5 else "shrink" if top10 > 18 else "no_objection"
        mult = {"block": 0.0, "shrink": 0.5, "no_objection": 1.0}[verdict]
        return Opinion(setup=setup, entry_window=window, conviction=round(conv, 2),
                       thesis=f"vol_accel {accel:.2f}, buy_sell_ratio_1h {bs:.2f}, drop_from_high_pct {drop:.0f}",
                       risks=[f"top10_pct {top10:.0f}"], verdict=verdict, size_multiplier=mult,
                       objection=f"top10_pct {top10:.0f} and fresh_share_pct {s.get('fresh_share_pct') or 0:.0f}",
                       change_mind="broader holder spread", cost_usd=0.07, model="simulated desk")
