import dataclasses
from datetime import datetime, timezone

import httpx
import pytest

from desk.ai import DeskUnavailable, Opinion
from desk.bot import Desk
from desk.config import load_config as _load_config
from desk.journal import Journal
from desk.sources import LimitedClient
from fomo.brokers import Fill, Quote
from fomo.chain import MintInfo
from fomo.swaps import Swap
from phantom.risk import KillSwitch

T0 = 1_760_000_000.0


def load_config(path="config/desk.yaml"):
    """The shipped gates and limits, with the desk's strict entry rules: these tests are about those rules, and
    config/desk.yaml may be running a looser experiment."""
    c = _load_config(path)
    return dataclasses.replace(c, desk=dataclasses.replace(c.desk, min_conviction=0.6, allowed_setups=["organic_trend", "early_breakout"],
                                                           allowed_windows=["early", "running"]))

GOOD, BAD = "Good" + "1" * 40, "Bad" + "1" * 41


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class World:
    """Fake sources: one price per mint, numbers that pass every gate unless a test changes them."""

    def __init__(self, clock):
        self.clock = clock
        self.price = {GOOD: 0.001, BAD: 0.001}
        self.dex_id = {GOOD: "pumpswap", BAD: "pumpswap"}
        self.mint_auth, self.no_sell, self.top10 = set(), set(), {}
        self.jup_hidden, self.dex_hidden = set(), set()  # mints Jupiter's price API / DexScreener leave out
        self.calls = {"holders": 0}

    # Jupiter tokens
    def jt(self, m):
        return {"id": m, "symbol": "GOOD" if m == GOOD else "BAD", "organicScore": 70, "holderCount": 3000,
                "firstPool": {"id": "pool" + m[:6], "createdAt": datetime.fromtimestamp(self.clock() - 3 * 3600, timezone.utc).isoformat()},
                "stats1h": {"holderChange": 5, "buyVolume": 1e5, "buyOrganicVolume": 7e4, "numTraders": 900},
                "audit": {"isSus": False, "mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 20,
                          "devBalancePercentage": 0}}

    def top(self, kind, window, limit):
        return [self.jt(m) for m in self.price]

    def recent(self):
        return []

    def search(self, mints):
        return {m: self.jt(m) for m in mints if m in self.price}

    # DexScreener
    def tokens(self, mints):
        out = {}
        for m in mints:
            if m not in self.price or m in self.dex_hidden:
                continue
            mc = self.price[m] * 1e9
            out[m] = {"chainId": "solana", "dexId": self.dex_id[m], "pairAddress": "pool" + m[:6], "baseToken": {"address": m, "symbol": "X"},
                      "priceUsd": str(self.price[m]),
                      "liquidity": {"usd": 150_000}, "_total_liquidity_usd": 150_000, "marketCap": mc, "pairCreatedAt": (self.clock() - 3 * 3600) * 1000,
                      "volume": {"m5": 30_000, "h1": 300_000, "h6": 1.2e6}, "txns": {"m5": {"buys": 60, "sells": 40}, "h1": {"buys": 700, "sells": 500}},
                      "priceChange": {"m5": 2, "h1": 15, "h6": 40, "h24": 80}, "boosts": {"active": 0}, "info": {}}
        return out

    def discovered(self):
        return []

    # GeckoTerminal: a steady uptrend near its high
    def candles(self, pool, minutes=5, limit=72, token=None):
        m = GOOD if pool == "pool" + GOOD[:6] else BAD
        p, t = self.price[m], self.clock()
        return [[t - (40 - i) * 300, p * (0.8 + i * 0.005), p * (0.81 + i * 0.005), p * (0.795 + i * 0.005), p * (0.805 + i * 0.005), 5000]
                for i in range(40)]

    # RugCheck / holders / mint
    def summary(self, mint):
        return {"score_normalised": 10}

    def holders_top(self, mint):
        self.calls["holders"] += 1
        t10 = self.top10.get(mint, 20.0)
        return [("w%d" % i, 4.0 if i == 0 else (t10 - 4) / 9) for i in range(10)]

    def mint_info(self, mint):
        return MintInfo(mint, 6, "A" * 44 if mint in self.mint_auth else None, None, "spl-token")

    def get(self, mints):
        return {m: self.price[m] for m in mints if m in self.price and m not in self.jup_hidden}


class Holders:
    def __init__(self, w):
        self.w = w

    def top(self, mint, pools=()):
        return self.w.holders_top(mint)

    def fresh_share(self, holders, hours, check=10):
        return 0.0, 10


class Broker:
    mode = "paper"

    def __init__(self, w, usd=100.0):
        self.w, self.cash, self.tok = w, usd, {}

    def usdc(self):
        return self.cash

    def sol(self):
        return 1.0

    def holdings(self):
        return dict(self.tok)

    def token_raw(self, mint):
        return self.tok.get(mint, 0)

    def quote_buy(self, mint, usd):
        return Quote("buy", mint, usd, int(usd / self.w.price[mint] * 1e6), int(usd / self.w.price[mint] * 1e6 * 0.99), 0.5)

    def quote_sell(self, mint, raw):
        if mint in self.w.no_sell:
            raise ValueError("no route")
        usd = raw / 1e6 * self.w.price[mint] * 0.99
        return Quote("sell", mint, usd, raw, int(usd * 0.99 * 1e6), 0.5)

    def execute(self, q):
        if q.side == "buy":
            self.cash -= q.usd
            self.tok[q.mint] = self.tok.get(q.mint, 0) + q.raw
            self.n = getattr(self, "n", 0) + 1
            return Fill("buy", q.mint, q.raw, q.usd, f"b{self.n}")
        raw = min(q.raw, self.tok.get(q.mint, 0))
        self.cash += q.usd * raw / q.raw
        self.tok[q.mint] -= raw
        self.n = getattr(self, "n", 0) + 1
        return Fill("sell", q.mint, raw, q.usd * raw / q.raw, f"s{self.n}")


class FakeDesk:
    def __init__(self, verdict="no_objection", mult=1.0, setup="organic_trend", conviction=0.8, fail=False):
        self.verdict, self.mult, self.setup, self.conviction, self.fail = verdict, mult, setup, conviction, fail
        self.calls = 0

    def spent_today(self):
        return 0.0

    def review(self, legend, snap, plan, symbol="", mint="", links=None):
        self.calls += 1
        assert "symbol" not in snap and all(isinstance(v, (int, float)) or v is None for v in snap.values())  # numbers only
        if self.fail:
            raise DeskUnavailable("down")
        return Opinion(setup=self.setup, entry_window="early", conviction=self.conviction, thesis="t", risks=[], verdict=self.verdict,
                       size_multiplier=self.mult, objection="o", change_mind="c")


def make(tmp_path, cfg=None, desk=None, **over):
    clock = Clock()
    w = World(clock)
    cfg = cfg or load_config("config/desk.yaml")
    d = Desk(mode="paper", cfg=cfg, broker=Broker(w), jup=w, dex=w, gecko=w, rug=w, holders=Holders(w), mint_info=w.mint_info,
             prices=w, desk=desk or FakeDesk(), journal=Journal(tmp_path / "j.sqlite"), kill=KillSwitch(tmp_path / "k.json"),
             clock=clock, threaded=False, **over)
    d.mark()
    return d, w, clock


def only_good(w):
    del w.price[BAD]


def tick(d, clock, seconds=10):
    clock.t += seconds
    d.step()


def test_a_clean_token_goes_through_every_gate_and_is_bought(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    f = d.journal.funnel("paper", "2025-10-09")
    assert f == {"universe": 1, "market": 1, "onchain": 1, "depth": 1, "window": 1, "desk": 1, "filled": 1}
    (p,) = d.journal.open_positions("paper")
    assert p["cost_usd"] == pytest.approx(3.0)  # 3% of $100
    assert p["station"] == "scout"
    assert len(d.journal.open_positions("paper", "shadow")) == 1  # the rules-only arm got it too


def test_skeptic_block_means_no_buy_but_a_shadow(tmp_path):
    d, w, clock = make(tmp_path, desk=FakeDesk(verdict="block", mult=0.0))
    only_good(w)
    d.sweep()
    assert d.journal.open_positions("paper") == []
    assert len(d.journal.open_positions("paper", "shadow")) == 1
    assert d.journal.decisions("paper")[0]["action"] == "skip"


def test_skeptic_shrink_halves_the_size_and_tiny_sizes_are_skipped(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, position_pct=10))
    d, w, clock = make(tmp_path, cfg=cfg, desk=FakeDesk(verdict="shrink", mult=0.5))
    only_good(w)
    d.sweep()
    assert d.journal.open_positions("paper")[0]["cost_usd"] == pytest.approx(5.0)
    d2, w2, _ = make(tmp_path / "b", desk=FakeDesk(verdict="shrink", mult=0.5))
    only_good(w2)
    d2.sweep()
    assert d2.journal.open_positions("paper") == []  # $3 x 0.5 is under the $3 minimum


@pytest.mark.parametrize("desk,why", [(FakeDesk(setup="late_chase"), "late_chase"), (FakeDesk(conviction=0.3), "conviction"),
                                      (FakeDesk(fail=True), "no desk opinion")])
def test_chief_skips_on_the_desks_objections_or_silence(tmp_path, desk, why):
    d, w, clock = make(tmp_path, desk=desk)
    only_good(w)
    d.sweep()
    assert d.journal.open_positions("paper") == []
    assert why in d.journal.decisions("paper")[0]["note"]


def test_rules_only_mode_trades_when_the_desk_is_down(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, desk=dataclasses.replace(cfg.desk, when_unavailable="rules_only"))
    d, w, clock = make(tmp_path, cfg=cfg, desk=FakeDesk(fail=True))
    only_good(w)
    d.sweep()
    assert len(d.journal.open_positions("paper")) == 1


def test_bonding_curve_tokens_die_at_the_market_gate_and_kills_count_once_a_day(tmp_path):
    d, w, clock = make(tmp_path)
    w.dex_id[BAD] = "pumpfun"
    d.sweep()
    clock.t += 120
    d.sweep()
    kills = {k["reason"]: k["n"] for k in d.journal.top_kills("paper", "2025-10-09")}
    assert kills.get("still on the pump.fun bonding curve or no pool") == 1


def test_facts_kill_and_cool_down(tmp_path):
    d, w, clock = make(tmp_path)
    w.mint_auth.add(BAD)
    w.top10[GOOD] = 50.0
    d.sweep()
    assert d.journal.open_positions("paper") == []
    n = w.calls["holders"]
    clock.t += 120
    d.sweep()
    assert w.calls["holders"] == n  # both cooled down: no new RPC holder checks


def test_honeypot_dies_at_the_depth_gate(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    w.no_sell.add(GOOD)
    d.sweep()
    assert d.journal.funnel("paper", "2025-10-09").get("depth", 0) == 0


def test_price_running_during_the_desk_review_cancels_the_buy(tmp_path):
    class RunningDesk(FakeDesk):
        def review(self, *a, **k):
            w.price[GOOD] *= 1.2
            return super().review(*a, **k)
    d, w, clock = make(tmp_path)
    d.desk = RunningDesk()
    only_good(w)
    d.sweep()
    assert d.journal.open_positions("paper") == []


def test_take_profit_then_trailing_stop_then_closed_with_profit(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    w.price[GOOD] = p["entry_price"] * 1.6
    tick(d, clock)
    p = d.journal.open_positions("paper")[0]
    assert p["sold_raw"] == pytest.approx(p["raw"] * 0.33, rel=0.02)
    w.price[GOOD] = p["entry_price"] * 1.05  # below the trailing stop, above breakeven floor? the floor is entry + 2%
    tick(d, clock)
    assert d.journal.open_positions("paper") == []
    (c,) = d.journal.closed_positions("paper")
    assert c["pnl"] > 0 and c["exit_kind"] == "trail"
    kinds = {r["kind"] for r in d.journal.exits_by_kind("paper")}
    assert kinds == {"rung", "trail"}


def test_stop_loss_and_pause_after_a_losing_streak(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, pause_after_losses=1))
    d, w, clock = make(tmp_path, cfg=cfg)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    w.price[GOOD] = p["entry_price"] * 0.5
    tick(d, clock)
    (c,) = d.journal.closed_positions("paper")
    assert c["exit_kind"] == "stop" and c["pnl"] < 0
    assert "paused" in d._buys_blocked()


def test_drawdown_fires_the_kill_switch_and_sells(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, position_pct=20, max_position_usd=50, max_exposure_pct=60, max_drawdown_pct=10),
                              plan=dataclasses.replace(cfg.plan, min_stop_pct=15, max_stop_pct=30))
    d, w, clock = make(tmp_path, cfg=cfg)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    w.price[GOOD] = p["entry_price"] * 0.45  # gaps through the stop: 20% of equity loses 55%
    tick(d, clock, 15)  # the manager sells at the stop, then the mark sees the drawdown...
    assert not d.kill.tripped
    tick(d, clock, 15)  # ...and the next mark still sees it: two in a row fire the kill switch
    assert d.kill.tripped and d.journal.open_positions("paper") == []
    assert d._buys_blocked()


def test_track_signal_is_a_candidate_and_the_wallets_sell_exits(tmp_path):
    W = "Track" + "1" * 39

    class Watcher:
        def __init__(self):
            self.queue = []

        def watch(self, wallets):
            self.wallets = wallets

        def poll(self):
            out, self.queue = self.queue, []
            return out
    watcher = Watcher()
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, track=dataclasses.replace(cfg.track, enabled=True))
    d, w, clock = make(tmp_path, cfg=cfg, watcher=watcher, tracked={W: "whale"})
    only_good(w)
    d.last["sweep"] = clock.t + 1e9  # no sweeps: only the tracked signal
    watcher.queue.append(Swap("s1", 1, int(clock.t), W, "buy", GOOD, 10**9, 6, 900.0, "USDC", 0, 10**9))
    tick(d, clock)
    tick(d, clock)
    (p,) = d.journal.open_positions("paper")
    assert p["station"] == "track" and p["source_wallet"] == W
    watcher.queue.append(Swap("s2", 2, int(clock.t), W, "sell", GOOD, 9 * 10**8, 6, 900.0, "USDC", 10**9, 10**8))
    tick(d, clock)
    assert d.journal.open_positions("paper") == []


def test_dashboard_state_has_what_the_page_reads(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    tick(d, clock, 20)
    s = d.dashboard_state()
    for k in ("mode", "day", "balance", "pnl", "equity", "funnel", "kills", "latest", "positions", "desk", "feed", "stations", "exits",
              "real", "shadow", "calibration", "ai", "limits", "health", "blocked", "scanState"):
        assert k in s
    assert s["latest"]["action"] == "buy" and s["positions"][0]["symbol"] == "GOOD"


# ---------------------------------------------------------------- what the reviews found
def test_a_coin_jupiter_stops_pricing_is_priced_from_dexscreener(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    w.jup_hidden.add(GOOD)
    w.price[GOOD] = p["entry_price"] * 0.5  # the crash Jupiter no longer shows
    tick(d, clock, 15)
    (c,) = d.journal.closed_positions("paper")
    assert c["exit_kind"] == "stop"


def test_a_coin_nobody_prices_or_buys_is_treated_as_dead(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    w.jup_hidden.add(GOOD)
    w.dex_hidden.add(GOOD)
    w.no_sell.add(GOOD)
    tick(d, clock, 15)
    assert d.journal.open_positions("paper") and GOOD in d.unpriced  # not yet: the sources may be catching up
    for _ in range(14):
        tick(d, clock, 10)
    assert d.equity_now == pytest.approx(d.broker.cash)  # valued at nothing, so the drawdown limits see the loss
    assert d.journal.open_positions("paper")[0]["exit_pending"]  # and it is being sold (no route yet, so retried)
    assert d.journal.open_positions("paper", "shadow") == []  # the rules-only arm booked it at zero
    assert not d.kill.tripped  # a 3% position going to zero is a loss, not an emergency


def test_kill_switch_during_the_requote_stops_the_buy(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    real_quote = d.broker.quote_buy
    calls = []

    def quote_buy(mint, usd):
        calls.append(1)
        if len(calls) == 2:  # the re-quote, after the desk said yes
            d.kill.trip("pressed while the desk was deciding")
        return real_quote(mint, usd)
    d.broker.quote_buy = quote_buy
    d.sweep()
    assert d.journal.open_positions("paper") == [] and d.broker.cash == 100.0


def test_a_coin_looked_at_again_gets_one_shadow_position(tmp_path):
    d, w, clock = make(tmp_path, desk=FakeDesk(verdict="block"))
    only_good(w)
    for _ in range(3):
        d.sweep()
        clock.t += d.cfg.scan.recheck_passed_minutes * 60 + 1
        d.prices_now.clear()
    assert d.desk.calls == 3 and len(d.journal.open_positions("paper", "shadow")) == 1


def test_one_coin_that_cant_be_sold_never_fires_the_kill_switch(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, position_pct=20, max_position_usd=50, max_exposure_pct=60))
    d, w, clock = make(tmp_path, cfg=cfg)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    real_execute = d.broker.execute

    def execute(q):
        if q.side == "sell":
            raise RuntimeError("transaction failed")
        return real_execute(q)
    d.broker.execute = execute
    w.price[GOOD] = p["entry_price"] * 0.8  # through the stop; the sell keeps failing
    for _ in range(40):
        tick(d, clock, 10)
    assert not d.kill.tripped
    (p,) = d.journal.open_positions("paper")
    assert p["exit_pending"] and d._retry_at.get(p["id"], 0) > clock.t  # worth more than dust: kept, retried once a minute


class LiveBroker(Broker):
    """A live wallet: balances can fail to read, a swap can land while its outcome comes back unknown, and settle()
    reads the landed swaps back the way the real one reads the wallet's transactions."""
    mode = "live"

    def __init__(self, w, usd=100.0):
        super().__init__(w, usd)
        self.reads_fail = False
        self.zero_reads = 0
        self.unknown = None  # None, "landed" or "lost": the next swap's outcome comes back unknown
        self.landed = []  # (t, Fill)
        self.chain_down = False

    def holdings(self):
        if self.reads_fail:
            raise RuntimeError("rpc down")
        h = dict(self.tok)
        h["EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"] = int(self.cash * 1e6)
        if self.zero_reads:
            self.zero_reads -= 1
            h.pop(GOOD, None)
        return h

    def token_raw(self, mint):
        return self.holdings().get(mint, 0)

    def usdc(self):
        return self.cash

    def execute(self, q):
        from phantom.execution.jupiter import SwapOutcomeUnknown
        how, self.unknown = self.unknown, None
        if how == "lost":
            raise SwapOutcomeUnknown("timed out")
        fill = super().execute(q)
        self.landed.append((self.w.clock(), fill))
        if how == "landed":
            raise SwapOutcomeUnknown("timed out")
        return fill

    def settle(self, mint, side, since, skip=()):
        if self.chain_down:
            raise RuntimeError("rpc down")
        return next((f for t, f in self.landed if t >= since - 5 and f.mint == mint and f.side == side and f.ref not in skip), None)


def live(tmp_path, cfg=None):
    d, w, clock = make(tmp_path, cfg=cfg)
    d.mode = "live"
    d.broker = LiveBroker(w)
    only_good(w)
    return d, w, clock


def test_live_buy_with_an_unknown_outcome_is_settled_from_the_chain_not_bought_twice(tmp_path):
    d, w, clock = live(tmp_path)
    d.broker.unknown = "landed"
    d.sweep()
    assert d.journal.open_positions("live") == [] and len(d.journal.pending("live")) == 1
    assert d.equity(d.broker.cash) == pytest.approx(100.0)  # the buy in flight still counts: no false drawdown
    clock.t += d.cfg.scan.recheck_passed_minutes * 60 + 1
    d.sweep()  # looked at again while it settles: not bought a second time
    assert len(d.broker.landed) == 1
    d.mark()
    (p,) = d.journal.open_positions("live")
    assert p["raw"] == d.broker.tok[GOOD] and d.journal.pending("live") == []


def test_live_buy_that_did_not_land_is_dropped_after_its_blockhash_expires(tmp_path):
    d, w, clock = live(tmp_path)
    d.broker.unknown = "lost"
    d.sweep()
    d.mark()
    assert len(d.journal.pending("live")) == 1  # too early to say
    clock.t += 200
    d.mark()
    assert d.journal.pending("live") == [] and d.journal.open_positions("live") == []


def test_a_crash_mid_buy_is_settled_at_start_up(tmp_path):
    d, w, clock = live(tmp_path)
    d.broker.unknown = "landed"  # the process dies right after sending: the pending row is all that's left
    d.sweep()
    d2 = Desk(mode="live", cfg=d.cfg, broker=d.broker, jup=w, dex=w, gecko=w, rug=w, holders=Holders(w), mint_info=w.mint_info,
              prices=w, desk=FakeDesk(), journal=d.journal, kill=d.kill, clock=clock, threaded=False)
    d2.mark()
    assert len(d2.journal.open_positions("live")) == 1 and d2.journal.pending("live") == []


def test_live_take_profit_with_an_unknown_outcome_is_not_sold_twice(tmp_path):
    d, w, clock = live(tmp_path)
    d.sweep()
    (p,) = d.journal.open_positions("live")
    w.price[GOOD] = p["entry_price"] * 1.6
    d.broker.unknown = "landed"
    tick(d, clock, 10)  # the rung's sell lands, but its outcome is unknown
    tick(d, clock, 10)  # the next round must not sell another rung while it settles
    sells = [f for _, f in d.broker.landed if f.side == "sell"]
    assert len(sells) == 1
    tick(d, clock, 15)
    (p2,) = d.journal.open_positions("live")
    assert p2["sold_raw"] == sells[0].raw and p2["exit_pending"] == 0
    assert {r["kind"] for r in d.journal.exits_by_kind("live")} == {"rung"}


def test_live_stop_still_sells_when_the_wallet_balance_cant_be_read(tmp_path):
    d, w, clock = live(tmp_path)
    d.sweep()
    (p,) = d.journal.open_positions("live")
    w.price[GOOD] = p["entry_price"] * 0.5
    d.broker.reads_fail = True
    tick(d, clock, 10)
    (c,) = d.journal.closed_positions("live")
    assert c["exit_kind"] == "stop" and d.broker.tok.get(GOOD, 0) == 0


def test_our_own_failures_never_write_a_sellable_coin_off(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")
    real_execute = d.broker.execute

    def execute(q):
        if q.side == "sell":
            raise RuntimeError("Swap failed: slippage")
        return real_execute(q)
    d.broker.execute = execute
    w.price[GOOD] = p["entry_price"] * 0.8  # a $3 position, now worth $2.40: through the stop
    for _ in range(40):
        tick(d, clock, 10)
    (p,) = d.journal.open_positions("paper")
    assert p["exit_pending"] and p["pending_kind"] == "stop"  # still held and still being sold
    d.broker.execute = real_execute
    clock.t += 61
    tick(d, clock, 10)
    (c,) = d.journal.closed_positions("paper")
    assert c["exit_kind"] == "stop"  # the retry keeps the real reason


def test_a_coin_with_no_route_and_no_price_is_written_off_with_its_loss_booked(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    w.jup_hidden.add(GOOD)
    w.dex_hidden.add(GOOD)
    w.no_sell.add(GOOD)
    for _ in range(50):
        tick(d, clock, 10)
    (c,) = d.journal.closed_positions("paper")
    assert c["pnl"] == pytest.approx(-c["cost_usd"])
    assert sum(r["pnl"] for r in d.journal.exits_by_kind("paper")) == pytest.approx(-c["cost_usd"])


def test_shadow_book_pays_the_paper_costs(tmp_path):
    d, w, clock = make(tmp_path, desk=FakeDesk(verdict="block"))
    only_good(w)
    d.sweep()
    (s,) = d.journal.open_positions("paper", "shadow")
    slip = d.cfg.paper.slippage_bps / 1e4
    assert s["exit_cost"] == pytest.approx(slip + 0.005)  # paper slippage + the buy quote's 0.5% impact
    assert s["raw"] / 1e6 * s["entry_price"] == pytest.approx(s["cost_usd"] * (1 - slip), rel=1e-4)


def test_sell_only_blocks_buys_and_reset_clears_streaks_in_the_running_process(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sell_only = "go-live checks fail"
    d.sweep()
    assert d.journal.open_positions("paper") == []
    for _ in range(3):
        clock.t += 16
        d._api_error("x", "swap")
    d.journal.put("reset_at:paper", 123)
    d.mark()
    assert d.api_errors == 0


def test_old_error_streaks_are_forgotten_at_start_up(tmp_path):
    d, w, clock = make(tmp_path)
    clock.t += 16
    d._api_error("x", "prices")
    clock.t += 3600
    d2 = Desk(mode="paper", cfg=d.cfg, broker=d.broker, jup=w, dex=w, gecko=w, rug=w, holders=Holders(w), mint_info=w.mint_info,
              prices=w, desk=FakeDesk(), journal=d.journal, kill=d.kill, clock=clock, threaded=False)
    assert d2.errors == {}


def test_live_one_zero_balance_read_does_not_close_a_position(tmp_path):
    d, w, clock = live(tmp_path)
    d.sweep()
    (p,) = d.journal.open_positions("live")
    clock.t += 300
    d.broker.zero_reads = 1
    d.mark()
    d.mark()
    assert d.journal.open_positions("live")  # one lagging read changes nothing
    d.broker.tok[GOOD] = p["raw"] // 2  # half was sold by hand in Phantom
    for _ in range(3):
        d.mark()
    (p2,) = d.journal.open_positions("live")
    assert p2["sold_raw"] == p["raw"] - p["raw"] // 2 and p2["cost_usd"] == p["cost_usd"]  # booked as an exit for nothing
    (x,) = [r for r in d.journal.exits_by_kind("live") if r["kind"] == "manual"]
    assert x["pnl"] == pytest.approx(-p["cost_usd"] / 2, rel=0.01)


def test_error_counts_survive_a_restart_per_source(tmp_path):
    d, w, clock = make(tmp_path)
    for i in range(3):
        clock.t += 16
        d._api_error("x", "prices")
    d2 = Desk(mode="paper", cfg=d.cfg, broker=d.broker, jup=w, dex=w, gecko=w, rug=w, holders=Holders(w), mint_info=w.mint_info,
              prices=w, desk=FakeDesk(), journal=d.journal, kill=d.kill, clock=clock, threaded=False)
    assert d2.errors == {"prices": 3}
    d2._api_ok("prices")
    assert d2.api_errors == 0


def test_one_stuck_sell_does_not_hold_up_the_other_stops(tmp_path):
    import threading
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, max_exposure_pct=60))
    clock = Clock()
    w = World(clock)
    d = Desk(mode="paper", cfg=cfg, broker=Broker(w), jup=w, dex=w, gecko=w, rug=w, holders=Holders(w), mint_info=w.mint_info,
             prices=w, desk=FakeDesk(), journal=Journal(tmp_path / "j.sqlite"), kill=KillSwitch(tmp_path / "k.json"),
             clock=clock, threaded=True)  # sells go to the worker pool
    d.mark()
    d.sweep()
    a, b = d.journal.open_positions("paper")
    release = threading.Event()
    real_execute = d.broker.execute

    def execute(q):
        if q.side == "sell" and q.mint == a["mint"]:
            release.wait(10)  # a swap stuck in Jupiter's confirmation poll
        return real_execute(q)
    d.broker.execute = execute
    for m in (GOOD, BAD):
        w.price[m] *= 0.5
    tick(d, clock, 10)
    for _ in range(100):
        if d.journal.closed_positions("paper"):
            break
        threading.Event().wait(0.02)
    assert [c["mint"] for c in d.journal.closed_positions("paper")] == [b["mint"]]  # B's stop sold while A's swap hangs
    assert d.busy()
    release.set()
    d._pool.shutdown(wait=True)
    assert len(d.journal.closed_positions("paper")) == 2 and not d.busy()


def test_a_dump_during_the_desk_review_cancels_the_buy(tmp_path):
    class DumpingDesk(FakeDesk):
        def review(self, *a, **k):
            w.price[GOOD] *= 0.4  # the creator sells into the pool while the desk thinks
            return super().review(*a, **k)
    d, w, clock = make(tmp_path, desk=DumpingDesk())
    only_good(w)
    assert d.evaluate(GOOD, "scout", w.jt(GOOD), w.tokens([GOOD])[GOOD], {}) == "ran"
    assert d.journal.open_positions("paper") == []


def test_the_requote_must_pass_the_depth_gate_again(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    real = d.broker.quote_buy
    calls = []

    def quote_buy(mint, usd):
        q = real(mint, usd)
        calls.append(1)
        return q if len(calls) == 1 else Quote(q.side, q.mint, q.usd, q.raw, int(q.raw * 0.7), 40.0)  # 40% impact now
    d.broker.quote_buy = quote_buy
    d.sweep()
    assert d.journal.open_positions("paper") == []


def test_rules_only_never_buys_just_because_the_desk_is_busy(tmp_path):
    cfg = load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, desk=dataclasses.replace(cfg.desk, when_unavailable="rules_only", max_reviews_per_hour=1))
    d, w, clock = make(tmp_path, cfg=cfg, desk=FakeDesk(verdict="block"))
    d.sweep()  # GOOD and BAD both pass the gates: the desk reviews (and blocks) one, the other finds the desk busy
    assert d.desk.calls == 1 and d.journal.open_positions("paper") == []


def test_one_coin_the_node_cant_read_is_set_aside_not_counted_until_the_kill_switch(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)

    def broken(mint):
        raise RuntimeError("Solana RPC getAccountInfo failed: not a token mint")
    d.mint_info_fn = broken
    for _ in range(40):  # 40 minutes: the coin is looked at again each recheck period, a few times in all
        clock.t += 61
        d.step()
    assert not d.kill.tripped and d.errors.get("rpc", 0) < d.cfg.risk.max_consecutive_api_errors


def test_mint_info_with_an_authority_is_read_again_on_the_next_look(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    w.mint_auth.add(GOOD)
    assert d._mint_info(GOOD).mint_authority
    w.mint_auth.discard(GOOD)  # the creator revoked it
    assert d._mint_info(GOOD).mint_authority is None
    assert d._mint_info(GOOD) is d._mint_info(GOOD)  # revoked for good: cached


def test_a_sell_whose_history_stays_unreadable_is_retried_from_the_wallet_balance(tmp_path):
    d, w, clock = live(tmp_path)
    d.sweep()
    (p,) = d.journal.open_positions("live")
    w.price[GOOD] = p["entry_price"] * 0.5
    d.broker.unknown = "lost"
    d.broker.chain_down = True  # getSignaturesForAddress keeps failing; plain balance reads still work
    tick(d, clock, 10)
    assert d.journal.pending("live") and d.journal.open_positions("live")
    for _ in range(40):
        tick(d, clock, 10)
    assert d.journal.pending("live") == [] and d.journal.open_positions("live") == []  # sold from the balance, not stuck
    assert d.broker.tok.get(GOOD, 0) == 0


def test_a_sell_booked_by_a_worker_during_the_wallet_read_skips_that_mark(tmp_path):
    d, w, clock = make(tmp_path)
    before = d.journal.equity_curve("paper")
    real_usdc = d.broker.usdc

    def usdc():
        d._book_version += 1  # a worker booked an exit meanwhile
        return real_usdc()
    d.broker.usdc = usdc
    clock.t += 15
    d.mark()
    assert len(d.journal.equity_curve("paper")) == len(before)


def test_a_buy_in_flight_never_raises_the_high_or_the_days_start(tmp_path):
    d, w, clock = make(tmp_path)
    d.journal.add_pending("paper", "buy", GOOD, None, 1, 20.0, clock(), {})  # USDC not yet left, coin not yet booked
    d._executing.add(1)
    clock.t += 15
    d.mark()
    assert d.equity_now == pytest.approx(120.0) and d.journal.get("peak:paper") == pytest.approx(100.0)


def test_pricing_trouble_with_a_rules_only_coin_never_fires_the_real_kill_switch(tmp_path):
    d, w, clock = make(tmp_path, desk=FakeDesk(verdict="block"))
    only_good(w)
    d.sweep()
    assert d.journal.open_positions("paper", "shadow") and not d.journal.open_positions("paper")
    w.jup_hidden.add(GOOD)

    def down(mints):
        raise RuntimeError("cloudflare")
    d.mdex.tokens = down
    for _ in range(30):
        tick(d, clock, 16)
    assert not d.kill.tripped


def test_an_old_tripped_kill_switch_survives_the_upgrade(tmp_path):
    from desk.__main__ import adopt_old_kill
    KillSwitch(tmp_path / "desk_kill.json").trip("pressed before the upgrade")
    new = KillSwitch(tmp_path / "desk_live_kill.json")
    adopt_old_kill(tmp_path, new)
    assert new.tripped and "before the upgrade" in new.reason


def test_a_scanner_only_outage_pauses_buying_and_resumes_by_itself_without_the_kill_switch(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)

    class Down(Exception):
        response = type("R", (), {"status_code": 401})()
    real_top = d.jup.top

    def broken(*a, **k):
        raise Down()
    d.jup.top = broken
    for _ in range(12):
        clock.t += 61
        d.sweep()
    assert not d.kill.tripped
    assert d.errors["jupiter"] >= 10 and "jupiter keeps failing" in d._buys_blocked()
    assert any("failed 10 times" in e["text"] for e in d.journal.events("paper", 20))
    d.jup.top = real_top  # it works again
    clock.t += 61
    d.sweep()
    assert d._buys_blocked() is None and len(d.journal.open_positions("paper")) == 1  # and the buy goes through


def test_a_failed_request_is_reported_with_its_http_status():
    from desk.bot import _why

    class E(Exception):
        response = type("R", (), {"status_code": 429})()
    assert _why(E()) == "HTTP 429" and _why(ValueError()) == "ValueError"


def test_a_network_outage_pauses_buying_but_never_fires_the_kill_switch(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    d.sweep()
    (p,) = d.journal.open_positions("paper")

    def down(mints):
        raise ConnectionError("ConnectError")
    real_get = d.prices.get
    d.prices.get = down
    for _ in range(40):  # about 10 minutes without a price
        tick(d, clock, 16)
    assert not d.kill.tripped and d.errors["prices"] >= 10 and "prices" in d._buys_blocked()
    assert d.journal.open_positions("paper")  # the coin is still held, not dumped
    d.prices.get = real_get  # the connection is back; and the coin has crashed meanwhile
    w.price[GOOD] = p["entry_price"] * 0.5
    tick(d, clock, 16)
    assert d._buys_blocked() is None
    (c,) = d.journal.closed_positions("paper")
    assert c["exit_kind"] == "stop"  # the stop fires on the first fresh price


def test_the_shipped_experiment_skips_unclear_and_a_30_percent_hour_and_buys_the_rest(tmp_path):
    """config/desk.yaml as shipped: only 'unclear' and a coin up more than 30% in the hour are turned away."""
    cfg = _load_config("config/desk.yaml")
    assert cfg.gates.max_price_change_1h_pct == 30
    for setup in ("unclear",):
        d, w, clock = make(tmp_path / setup, cfg=cfg, desk=FakeDesk(setup=setup, conviction=0.2))
        only_good(w)
        d.sweep()
        assert d.journal.open_positions("paper") == [], setup
    for setup in ("late_chase", "fading", "organic_trend"):
        d, w, clock = make(tmp_path / setup, cfg=cfg, desk=FakeDesk(setup=setup, conviction=0.2))
        only_good(w)
        d.sweep()
        assert len(d.journal.open_positions("paper")) == 1, setup  # bought, whatever the confidence
    # a coin that rose 31% in the hour never reaches the desk
    d, w, clock = make(tmp_path / "hot", cfg=cfg)
    only_good(w)
    real = w.tokens
    w.tokens = lambda mints: {m: {**p, "priceChange": {**p["priceChange"], "h1": 31}} for m, p in real(mints).items()}
    d.sweep()
    assert d.journal.open_positions("paper") == [] and d.desk.calls == 0


# ---------------------------------------------------------------- wider and faster scan
def test_the_scan_is_faster_and_wider_than_before():
    c = _load_config("config/desk.yaml")
    assert c.scan.interval_seconds == 30 and c.scan.max_universe == 600 and c.scan.deep_checks_per_sweep == 6
    assert "toptraded/1h" in c.scan.extra_jupiter_lists and c.scan.gecko_discovery_every == 4


def test_gecko_trending_and_new_pools_join_the_universe_and_their_ranks_reach_the_snapshot(tmp_path):
    seen = []

    class Watching(FakeDesk):
        def review(self, legend, snap, plan, symbol="", mint="", links=None):
            seen.append(snap)
            return super().review(legend, snap, plan, symbol, mint, links)
    d, w, clock = make(tmp_path, desk=Watching())
    only_good(w)
    w.pools = lambda kind: [{"mint": GOOD, "pool": "pool" + GOOD[:6], "rank": 3 if kind == "trending_pools" else 1}]
    d.gecko = w
    d.sweep()
    (snap,) = seen
    assert snap["trending_rank_gecko"] == 3 and snap["new_listing"] == 1
    assert "trending_rank_gecko" in __import__("desk.features", fromlist=["LEGEND"]).LEGEND


def test_a_coin_only_gecko_knows_is_found_and_checked(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    w.top = lambda kind, window, limit: []  # Jupiter's lists are empty; only GeckoTerminal points at the coin
    w.pools = lambda kind: [{"mint": GOOD, "pool": "", "rank": 1}] if kind == "trending_pools" else []
    d.sweep()
    assert len(d.journal.open_positions("paper")) == 1


def test_discovery_trouble_is_quiet_and_dead_lists_are_dropped(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)

    class NotFound(Exception):
        response = type("R", (), {"status_code": 404})()
    calls = []
    real_top = w.top

    def top(kind, window, limit):
        calls.append((kind, window))
        if kind == "toptraded":
            raise NotFound()
        return real_top(kind, window, limit)

    def pools(kind):
        raise RuntimeError("GeckoTerminal down")
    w.top, w.pools = top, pools
    for _ in range(7):
        clock.t += 31
        d.sweep()
    assert not d.kill.tripped and d.errors.get("jupiter", 0) == 0 and not d.errors.get("scan")
    assert [c for c in calls if c[0] == "toptraded"] == [("toptraded", "1h")]  # asked once, then dropped
    assert any(c == ("toptrending", "1h") for c in calls)  # the other extra lists still run


def test_the_most_watched_coins_get_the_deep_checks_first():
    from desk.bot import Desk
    quiet = {"vol_accel": 1.0, "organic_score": 60}
    watched = {**quiet, "trending_rank_gecko": 4, "trending_rank_jupiter": 9, "new_listing": 1}
    assert Desk._priority(watched) == pytest.approx(60 * 1.5 * 1.2 * 1.2)
    assert Desk._priority(watched) > Desk._priority({**quiet, "vol_accel": 1.5})
    assert Desk._priority({**quiet, "trending_rank_jupiter": 80}) == pytest.approx(60)  # far down the list: no bonus


# ---------------------------------------------------------------- fixes from the review of the wider scan
def test_attention_facts_age_on_their_own_and_are_never_lost_by_a_sweep_that_didnt_pull_them(tmp_path):
    seen = []

    class Watching(FakeDesk):
        def review(self, legend, snap, plan, symbol="", mint="", links=None):
            seen.append(snap)
            return super().review(legend, snap, plan, symbol, mint, links)
    d, w, clock = make(tmp_path, desk=Watching())
    only_good(w)
    w.pools = lambda kind: [{"mint": GOOD, "pool": "", "rank": 3 if kind == "trending_pools" else 1}]
    d.sweep()  # sweep 1 pulls GeckoTerminal; sweep 2 doesn't
    assert (seen[0]["trending_rank_gecko"], seen[0]["new_listing"]) == (3, 1)
    clock.t += 31
    now = clock()
    assert d._attention(GOOD, now)["trending_rank_gecko"] == 3 and d._attention(GOOD, now)["new_listing"] == 1  # still known
    d.sweep()
    d._cool.clear()
    assert d._attention(GOOD, clock.t + 500)["trending_rank_gecko"] is None  # a rank goes stale after 8 minutes
    assert d._attention(GOOD, clock.t + 500)["new_listing"] == 1  # "newly listed" lasts the hour
    assert d._attention(GOOD, clock.t + 3700) == {"trending_rank_gecko": None, "trending_rank_jupiter": None, "new_listing": 0}


def test_g2_is_cheapest_first(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    w.mint_auth.add(GOOD)
    fresh_calls = []
    real_fresh = d.holders.fresh_share
    d.holders.fresh_share = lambda *a, **k: (fresh_calls.append(1), real_fresh(*a, **k))[1]
    d.sweep()
    assert w.calls["holders"] == 0 and not fresh_calls  # died on its mint authority: no holder lookups at all
    w.mint_auth.clear()
    d._cool.clear()
    w.top10[GOOD] = 60.0
    d.sweep()
    assert w.calls["holders"] == 1 and not fresh_calls  # died on concentration: no wallet-age lookups
    w.top10[GOOD] = 20.0
    d._cool.clear()
    d.sweep()
    assert fresh_calls and len(d.journal.open_positions("paper")) == 1  # a survivor pays for everything


def test_a_candle_failure_is_not_a_verdict_on_the_coin(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)

    def down(*a, **k):
        raise RuntimeError("429")
    real = w.candles
    w.candles = down
    d.sweep()
    assert not d.journal.open_positions("paper")
    assert d.journal.funnel("paper", "2025-10-09").get("window", 0) == 0  # not counted as a window kill
    assert 0 < d._cool[GOOD] - clock() <= 120  # looked at again in two minutes, not half an hour
    w.candles = real
    clock.t += 121
    d.sweep()
    assert len(d.journal.open_positions("paper")) == 1


def test_geckoterminal_never_makes_a_call_wait_long_on_a_429():
    slept = []
    calls = []

    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(429, headers={"retry-after": "30"}) if len(calls) % 2 == 1 else httpx.Response(200, json={})
    c = LimitedClient(rates={"api.geckoterminal.com": 1000.0, "api.jup.ag": 1000.0}, sleep=slept.append, transport=httpx.MockTransport(handler))
    c.get("https://api.geckoterminal.com/api/v2/x")
    c.get("https://api.jup.ag/tokens/v2/x")
    assert [x for x in slept if x >= 1] == [3.0, 30.0]  # Gecko: capped at 3 s; Jupiter keeps the general cap


def test_the_universe_cap_keeps_the_coins_people_are_looking_at(tmp_path):
    cfg = _load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, scan=dataclasses.replace(cfg.scan, max_universe=1))
    d, w, clock = make(tmp_path, cfg=cfg)
    w.top = lambda kind, window, limit: [] if kind == "toptrending" else [w.jt(BAD)]  # Jupiter's organic list has BAD first...
    w.pools = lambda kind: [{"mint": GOOD, "pool": "", "rank": 1}] if kind == "trending_pools" else []  # ...but people watch GOOD
    d.sweep()
    assert [p["mint"] for p in d.journal.open_positions("paper")] == [GOOD]


def test_a_list_the_server_does_not_know_is_asked_again_after_an_hour(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)

    class NotFound(Exception):
        response = type("R", (), {"status_code": 404})()
    asked = []
    real_top = w.top

    def top(kind, window, limit):
        asked.append(kind)
        if kind == "toptraded":
            raise NotFound()
        return real_top(kind, window, limit)
    w.top = top
    for i in range(7):
        clock.t += 31
        d.sweep()
    assert asked.count("toptraded") == 1
    clock.t += 3700
    for i in range(3):
        clock.t += 31
        d.sweep()
    assert asked.count("toptraded") == 2


def test_no_buy_possible_means_no_deep_checks_and_no_skip_spam(tmp_path):
    cfg = _load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, max_open_positions=1, position_pct=20, max_position_usd=50))
    d, w, clock = make(tmp_path, cfg=cfg)
    only_good(w)
    d.sweep()
    assert len(d.journal.open_positions("paper")) == 1
    w.price[BAD] = 0.001
    before = len(d.journal.events("paper", 500))
    holders_before = w.calls["holders"]
    for _ in range(5):
        clock.t += 31
        d.sweep()
    skips = [e for e in d.journal.events("paper", 500)[: len(d.journal.events("paper", 500)) - before] if e["kind"] == "SKIP"]
    assert w.calls["holders"] == holders_before and not skips


def test_a_slow_sweep_stops_starting_deep_checks_and_picks_up_next_sweep(tmp_path):
    class Slow(FakeDesk):
        def review(self, *a, **k):
            clock.t += 100  # a review that takes longer than the whole sweep interval
            return super().review(*a, **k)
    cfg = _load_config("config/desk.yaml")
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, max_open_positions=4, position_pct=10, max_position_usd=30))
    d, w, clock = make(tmp_path, cfg=cfg, desk=Slow(verdict="block"))
    w.price[BAD] = 0.001
    d.sweep()
    assert d.desk.calls == 1  # the second finalist waits for the next sweep
    clock.t += 31
    d._cool.pop(GOOD, None)
    d.sweep()
    assert d.desk.calls == 2
