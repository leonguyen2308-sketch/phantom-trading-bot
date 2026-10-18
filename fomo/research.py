"""Before risking money: what would copying these traders have done over the last week?

1. Read each trader's real trades from the chain (their wallet's transactions, parsed like the live bot does).
2. Score the traders on their own trades: round trips, win rate, realized P&L.
3. Replay the video's rule on that history: copy the #1 trader at 0.25x, exit when they sell, fire after
   2 losses in a row. Our fills are the trader's prices made worse by a slippage you pick, because a
   copy lands seconds later. Run at several slippages to see how much the result depends on speed.

Limits: prices between a trader's trades are unknown, so stop losses and max-hold exits can't be
replayed; copies still open at the end are valued at the last price anyone in the data traded.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from fomo.chain import RpcError, SolanaRpc
from fomo.config import FomoConfig
from fomo.leaderboard import Trader
from fomo.rules import Rotation
from fomo.swaps import Swap, parse_swap
from fomo.watcher import usdc_account

log = logging.getLogger(__name__)

SLIPPAGES = (0.0, 0.03, 0.08)  # per side
START_USD = 100.0


class SwapCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS swaps (sig TEXT, wallet TEXT, swap TEXT, PRIMARY KEY (sig, wallet))")

    def get(self, sig: str, wallet: str):
        row = self.db.execute("SELECT swap FROM swaps WHERE sig=? AND wallet=?", (sig, wallet)).fetchone()
        if row is None:
            return False
        return Swap(**json.loads(row[0])) if row[0] != "null" else None

    def put(self, sig: str, wallet: str, sw: Swap | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO swaps VALUES (?,?,?)", (sig, wallet, json.dumps(sw.to_dict() if sw else None)))
        self.db.commit()


def fetch_swaps(rpc: SolanaRpc, wallet: str, since: float, sol_usd: float, max_tx: int, cache: SwapCache,
                progress=lambda n: None) -> list[Swap]:
    """The wallet's trades since `since`. Reads both the wallet and its USDC account: an app that signs
    for its users (FOMO does) may not name the wallet itself in every trade."""
    found: dict[str, int] = {}
    for addr in (wallet, usdc_account(wallet)):
        before = None
        while len(found) < max_tx * 2:
            page = rpc.signatures(addr, limit=100, before=before)
            if not page:
                break
            for s in page:
                if s.get("err") is None and (s.get("blockTime") or 0) >= since:
                    found.setdefault(s["signature"], s.get("blockTime") or 0)
            if (page[-1].get("blockTime") or 0) < since or len(page) < 100:
                break
            before = page[-1]["signature"]
    sigs = sorted(found, key=found.get, reverse=True)[:max_tx]
    out: list[Swap] = []
    for i, sig in enumerate(sigs, 1):
        hit = cache.get(sig, wallet)
        if hit is False:
            try:
                hit = parse_swap(rpc.transaction(sig), wallet, sol_usd)
            except RpcError as e:
                log.warning("skipped %s: %s", sig[:12], e)
                continue
            cache.put(sig, wallet, hit)
        if hit:
            out.append(hit)
        progress(i / max(len(sigs), 1))
    return sorted(out, key=lambda s: (s.t, s.slot))


def trader_stats(swaps: list[Swap]) -> dict:
    """Round trips on the trader's own fills (average cost). Sells of coins bought before the window are skipped."""
    hold: dict[str, list] = {}  # mint -> [raw, cost_usd, opened_t, realized]
    trips: list[tuple[float, float]] = []  # (pnl, hours held)
    for s in swaps:
        h = hold.setdefault(s.mint, [0, 0.0, s.t, 0.0])
        if s.side == "buy":
            if h[0] <= 0:
                h[:] = [0, 0.0, s.t, 0.0]
            h[0] += s.raw
            h[1] += s.usd
        elif h[0] > 0:
            part = min(1.0, s.raw / h[0])
            h[3] += s.usd - h[1] * part
            h[0] -= min(s.raw, h[0])
            h[1] *= 1 - part
            if h[0] <= 0 or s.post_raw == 0:
                trips.append((h[3], (s.t - h[2]) / 3600))
                h[:] = [0, 0.0, s.t, 0.0]
    wins = sum(1 for p, _ in trips if p > 0)
    return {"swaps": len(swaps), "buys": sum(1 for s in swaps if s.side == "buy"), "round_trips": len(trips),
            "win_rate": round(wins / len(trips), 3) if trips else None, "realized_usd": round(sum(p for p, _ in trips), 2),
            "median_hold_h": round(statistics.median(h for _, h in trips), 2) if trips else None}


def replay(traders: list[Trader], swaps: dict[str, list[Swap]], cfg: FomoConfig, slip: float, fee: float = 0.005) -> dict:
    """The copy rule on real history. Returns the equity path, every copy and every firing."""
    rot = Rotation(cfg.copy.fire_after_losses, cfg.copy.fire_cooldown_days * 86400)
    events = sorted((s for w in swaps.values() for s in w), key=lambda s: (s.t, s.slot))
    if not events:
        return {"slippage": slip, "copies": 0, "end_usd": START_USD, "pnl": 0.0, "fires": [], "unresolved": 0}
    rot.set_lineup(traders, events[0].t)
    cash = START_USD
    pos: dict[str, dict] = {}  # mint -> {trader, tokens, cost, sold}
    last_px: dict[str, float] = {}
    closed, fires = [], []
    for s in events:
        last_px[s.mint] = s.price
        if s.side == "buy" and s.wallet == rot.s["following"]:
            p = pos.get(s.mint)
            if p is None and len(pos) >= cfg.limits.max_open_positions:
                continue
            equity = cash + sum(q["tokens"] * last_px.get(m, 0) for m, q in pos.items())
            usd = min(s.usd * cfg.copy.copy_ratio, cfg.copy.max_trade_usd, equity * cfg.limits.max_position_pct, cash)
            if usd < cfg.copy.min_trade_usd:
                continue
            tokens = usd * (1 - fee) / (s.price * (1 + slip))
            cash -= usd
            if p:
                p["tokens"] += tokens
                p["cost"] += usd
            else:
                pos[s.mint] = {"trader": s.wallet, "tokens": tokens, "cost": usd, "sold": 0.0, "t": s.t}
        elif s.side == "sell" and s.mint in pos and pos[s.mint]["trader"] == s.wallet:
            p = pos[s.mint]
            frac = 1.0 if s.post_raw == 0 else s.sold_fraction
            sell_tokens = p["tokens"] * frac
            got = sell_tokens * s.price * (1 - slip) * (1 - fee)
            cash += got
            p["tokens"] -= sell_tokens
            p["sold"] += got
            if frac >= 0.999 or p["tokens"] * s.price < 0.5:
                cash += p["tokens"] * s.price * (1 - slip) * (1 - fee)
                pnl = p["sold"] + p["tokens"] * s.price * (1 - slip) * (1 - fee) - p["cost"]
                del pos[s.mint]
                closed.append({"t": s.t, "trader": s.wallet, "pnl": round(pnl, 4)})
                fired, ev = rot.record(s.wallet, pnl, s.t)
                if fired:
                    fires.append({"t": s.t, "events": ev})
    open_value = sum(q["tokens"] * last_px.get(m, 0) for m, q in pos.items())
    end = cash + open_value
    wins = sum(1 for c in closed if c["pnl"] > 0)
    return {"slippage": slip, "copies": len(closed) + len(pos), "closed": len(closed), "wins": wins,
            "win_rate": round(wins / len(closed), 3) if closed else None, "unresolved": len(pos),
            "end_usd": round(end, 2), "pnl": round(end - START_USD, 2), "fires": fires,
            "following_at_end": rot.s["following"], "per_trader": {w: rot.stats(w) for w in swaps}}


def run_research(traders: list[Trader], rpc: SolanaRpc, cfg: FomoConfig, *, days: float, sol_usd: float, max_tx: int,
                 cache: Path, out: Path) -> dict:
    now = datetime.now(timezone.utc).timestamp()
    since = now - days * 86400
    sc = SwapCache(cache)
    swaps: dict[str, list[Swap]] = {}
    print(f"Reading up to {max_tx} transactions per trader from the last {days:g} days (cached in {cache})...")
    for t in traders:
        print(f"  @{t.handle:18}", end="", flush=True)
        swaps[t.wallet] = fetch_swaps(rpc, t.wallet, since, sol_usd, max_tx, sc,
                                      progress=lambda f: print(f"\r  @{t.handle:18} {f:5.0%}", end="", flush=True))
        print(f"\r  @{t.handle:18} {len(swaps[t.wallet])} trades")
    stats = {t.wallet: trader_stats(swaps[t.wallet]) for t in traders}
    print("\nEach trader's own trades (what the leaderboard doesn't show you):")
    print(f"  {'trader':20} {'trades':>6} {'round trips':>11} {'win rate':>8} {'realized':>12} {'median hold':>11}")
    for t in traders:
        s = stats[t.wallet]
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "-"
        mh = f"{s['median_hold_h']:.1f}h" if s["median_hold_h"] is not None else "-"
        print(f"  @{t.handle:19} {s['swaps']:6d} {s['round_trips']:11d} {wr:>8} {s['realized_usd']:+12,.0f} {mh:>11}")
    results = [replay(traders, swaps, cfg, slip) for slip in SLIPPAGES]
    print(f"\nCopying with the video's rule (copy {cfg.copy.copy_ratio:g}x, max ${cfg.copy.max_trade_usd:g}/copy, "
          f"fire after {cfg.copy.fire_after_losses} L in a row), starting with ${START_USD:.0f}:")
    print(f"  {'slippage/side':>13} {'copies':>6} {'win rate':>8} {'fired':>5} {'end':>10} {'P&L':>10}")
    for r in results:
        wr = f"{r['win_rate']:.0%}" if r.get("win_rate") is not None else "-"
        print(f"  {r['slippage']:13.0%} {r['copies']:6d} {wr:>8} {len(r['fires']):5d} {r['end_usd']:10,.2f} {r['pnl']:+10,.2f}")
    print("\n  0% = you fill at exactly the trader's price (impossible). 3% is a fast bot on a liquid coin.")
    print("  8% is closer to a home Mac copying a hyped coin. Stop losses are not replayed (see FOMO_BOT.md).")
    report = {"at": int(now), "days": days, "sol_usd": sol_usd, "traders": [t.to_dict() for t in traders],
              "trader_stats": stats, "replay": results}
    out.mkdir(exist_ok=True)
    (out / "fomo_research.json").write_text(json.dumps(report, indent=1))
    print("\nSaved reports/fomo_research.json")
    return report
