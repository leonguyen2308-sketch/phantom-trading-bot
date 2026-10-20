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
            if m not in self.price:
                continue
            mc = self.price[m] * 1e9
            out[m] = {"chainId": "solana", "dexId": self.dex_id[m], "pairAddress": "pool" + m[:6], "baseToken": {"address": m, "symbol": "X"},
                      "liquidity": {"usd": 150_000}, "_total_liquidity_usd": 150_000, "marketCap": mc, "pairCreatedAt": (self.clock() - 3 * 3600) * 1000,
                      "volume": {"m5": 30_000, "h1": 300_000, "h6": 1.2e6}, "txns": {"m5": {"buys": 60, "sells": 40}, "h1": {"buys": 700, "sells": 500}},
                      "priceChange": {"m5": 2, "h1": 15, "h6": 40, "h24": 80}, "boosts": {"active": 0}, "info": {}}
        return out

    def discovered(self):
        return []

    # GeckoTerminal: a steady uptrend near its high
    def candles(self, pool, minutes=5, limit=72):
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
        return {m: self.price[m] for m in mints if m in self.price}


class Holders:
    def __init__(self, w):
        self.w = w

    def top(self, mint):
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
