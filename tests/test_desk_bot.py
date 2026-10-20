import dataclasses
from datetime import datetime, timezone

import pytest

from desk.ai import DeskUnavailable, Opinion
from desk.bot import Desk
from desk.config import load_config
from desk.journal import Journal
from fomo.brokers import Fill, Quote
from fomo.chain import MintInfo
from fomo.swaps import Swap
from phantom.risk import KillSwitch

T0 = 1_760_000_000.0
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
            return Fill("buy", q.mint, q.raw, q.usd, "b")
        raw = min(q.raw, self.tok.get(q.mint, 0))
        self.cash += q.usd * raw / q.raw
        self.tok[q.mint] -= raw
        return Fill("sell", q.mint, raw, q.usd * raw / q.raw, "s")


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
    tick(d, clock, 15)  # the manager sells at the stop, then the mark sees the drawdown
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

    def settle(self, mint, side, since):
        if self.chain_down:
            raise RuntimeError("rpc down")
        return next((f for t, f in self.landed if t >= since - 5 and f.mint == mint and f.side == side), None)


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
    for _ in range(15):
        clock.t += 61
        d.step()
    assert not d.kill.tripped and d.errors.get("rpc", 0) <= 1


def test_mint_info_with_an_authority_is_read_again_on_the_next_look(tmp_path):
    d, w, clock = make(tmp_path)
    only_good(w)
    w.mint_auth.add(GOOD)
    assert d._mint_info(GOOD).mint_authority
    w.mint_auth.discard(GOOD)  # the creator revoked it
    assert d._mint_info(GOOD).mint_authority is None
    assert d._mint_info(GOOD) is d._mint_info(GOOD)  # revoked for good: cached
