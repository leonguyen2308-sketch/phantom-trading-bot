import dataclasses

import pytest

from fomo.bot import CopyBot
from fomo.brokers import Fill, Quote
from fomo.chain import MintInfo
from fomo.config import load_config
from fomo.journal import Journal
from fomo.leaderboard import Trader
from fomo.market import TokenStats
from fomo.swaps import Swap
from phantom.risk import KillSwitch

A, B, C = "A" * 44, "B" * 44, "C" * 44
T0 = 1_700_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeWatcher:
    ws_connected = True

    def __init__(self):
        self.queue, self.watched = [], set()

    def watch(self, wallets):
        self.watched = set(wallets)

    def poll(self):
        out = [s for s in self.queue if s.wallet in self.watched]
        self.queue = []
        return out


class Market:
    """Prices per mint; the fake broker fills at them."""

    def __init__(self):
        self.px, self.liq, self.freeze, self.no_sell = {}, {}, set(), set()

    def get(self, mints):
        return {m: self.px[m] for m in mints if m in self.px}


class Dex:
    def __init__(self, m):
        self.m = m

    def get(self, mint):
        return TokenStats(mint, mint[:3], self.m.px[mint], self.m.liq.get(mint, 1e6), 5e6, 0)


class Broker:
    mode = "paper"

    def __init__(self, m, usd=100.0):
        self.m, self.cash, self.tok = m, usd, {}

    def usdc(self):
        return self.cash

    def sol(self):
        return 1.0

    def token_raw(self, mint):
        return self.tok.get(mint, 0)

    def holdings(self):
        return dict(self.tok)

    def quote_buy(self, mint, usd):
        return Quote("buy", mint, usd, int(usd / self.m.px[mint] * 1e6), 0, 0.1)

    def quote_sell(self, mint, raw):
        if mint in self.m.no_sell:
            raise ValueError("no route")
        return Quote("sell", mint, raw / 1e6 * self.m.px[mint], raw, 0, 0.1)

    def execute(self, q):
        if q.side == "buy":
            self.cash -= q.usd
            self.tok[q.mint] = self.tok.get(q.mint, 0) + q.raw
            return Fill("buy", q.mint, q.raw, q.usd, "x")
        raw = min(q.raw, self.tok.get(q.mint, 0))
        self.cash += q.usd * raw / q.raw
        self.tok[q.mint] -= raw
        return Fill("sell", q.mint, raw, q.usd * raw / q.raw, "x")


@pytest.fixture
def world(tmp_path):
    clock, m, w = Clock(), Market(), FakeWatcher()
    cfg = load_config("config/fomo.yaml")
    bot = CopyBot(mode="paper", cfg=cfg, broker=Broker(m), watcher=w, prices=m, dex=Dex(m),
                  mint_info=lambda mint: MintInfo(mint, 6, None, "F" * 44 if mint in m.freeze else None, "spl-token"),
                  lineup=lambda: [Trader("ace", A, 1), Trader("bee", B, 2), Trader("cat", C, 3)],
                  journal=Journal(tmp_path / "j.sqlite"), kill=KillSwitch(tmp_path / "k.json"), clock=clock)
    bot.step()
    return bot, m, w, clock


n = 0


def trade(wallet, side, mint, usd, price, t, pre=0, post=None):
    global n
    n += 1
    raw = int(usd / price * 1e6)
    if side == "sell" and post is None:
        post = 0
        raw = pre
    return Swap(f"s{n}", n, int(t), wallet, side, mint, raw, 6, usd, "USDC", pre, raw if side == "buy" and post is None else post)


def run(bot, w, clock, *swaps, advance=1):
    clock.t += advance
    w.queue += list(swaps)
    bot.step()


def mint(i):
    return f"Mint{i}".ljust(44, "x")


def test_copies_the_followed_traders_buy_at_a_quarter_size_capped(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t))
    (c,) = bot.journal.open_copies("paper")
    assert c["cost_usd"] == pytest.approx(10.0)  # 40 x 0.25
    m.px[mint(2)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(2), 4000, 0.01, clock.t))
    assert bot.journal.open_copies("paper")[1]["cost_usd"] == pytest.approx(20.0)  # capped by max_trade_usd


def test_ignores_traders_we_dont_copy(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    w.watched.add(B)  # even if their trade reaches us
    run(bot, w, clock, trade(B, "buy", mint(1), 40, 0.01, clock.t))
    assert bot.journal.open_copies("paper") == []


def test_sells_the_same_share_the_trader_sold(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    buy = trade(A, "buy", mint(1), 40, 0.01, clock.t)
    run(bot, w, clock, buy)
    m.px[mint(1)] = 0.02
    half = Swap("h", 99, int(clock.t), A, "sell", mint(1), buy.raw // 2, 6, 40, "USDC", buy.raw, buy.raw - buy.raw // 2)
    run(bot, w, clock, half)
    (c,) = bot.journal.open_copies("paper")
    assert c["sold_raw"] == pytest.approx(c["raw"] / 2, rel=0.01)
    run(bot, w, clock, Swap("h2", 100, int(clock.t), A, "sell", mint(1), buy.raw - buy.raw // 2, 6, 40, "USDC", buy.raw - buy.raw // 2, 0))
    assert bot.journal.open_copies("paper") == []
    (done,) = bot.journal.closed_copies("paper")
    assert done["pnl"] == pytest.approx(10.0)  # bought $10 at 0.01, sold all at 0.02


def lose(bot, m, w, clock, wallet, i):
    m.px[mint(i)] = 0.01
    b = trade(wallet, "buy", mint(i), 40, 0.01, clock.t)
    run(bot, w, clock, b)
    m.px[mint(i)] = 0.008
    run(bot, w, clock, trade(wallet, "sell", mint(i), 30, 0.008, clock.t, pre=b.raw))


def test_two_losses_in_a_row_fire_the_trader_and_copy_the_next(world):
    bot, m, w, clock = world
    lose(bot, m, w, clock, A, 1)
    assert bot.rot.following.handle == "ace"
    lose(bot, m, w, clock, A, 2)
    assert bot.rot.following.handle == "bee"
    kinds = [e["kind"] for e in bot.journal.events("paper")]
    assert "FIRE" in kinds
    m.px[mint(3)] = 0.01
    run(bot, w, clock, trade(B, "buy", mint(3), 40, 0.01, clock.t), trade(A, "buy", mint(4), 40, 0.01, clock.t))
    assert [c["trader"] for c in bot.journal.open_copies("paper")] == [B]


def test_a_fired_traders_open_copy_still_exits_when_they_sell(world):
    bot, m, w, clock = world
    m.px[mint(9)] = 0.01
    keep = trade(A, "buy", mint(9), 40, 0.01, clock.t)
    run(bot, w, clock, keep)
    lose(bot, m, w, clock, A, 1)
    lose(bot, m, w, clock, A, 2)
    assert bot.rot.following.handle == "bee"
    assert A in bot.watched_wallets()
    run(bot, w, clock, trade(A, "sell", mint(9), 40, 0.01, clock.t, pre=keep.raw))
    assert bot.journal.open_copies("paper") == []


@pytest.mark.parametrize("setup,why", [
    (lambda m, k: m.freeze.add(k), "freeze"),
    (lambda m, k: m.liq.__setitem__(k, 1000), "liquidity"),
    (lambda m, k: m.no_sell.add(k), "honeypot"),
    (lambda m, k: m.px.__setitem__(k, 0.02), "above the trader"),
])
def test_safety_checks_skip_the_copy(world, setup, why):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    setup(m, mint(1))
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t))
    assert bot.journal.open_copies("paper") == []
    assert why in bot.journal.events("paper")[0]["text"]


def test_late_signals_are_not_copied(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t - 300))
    assert bot.journal.open_copies("paper") == []


def test_stop_loss_sells(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t))
    m.px[mint(1)] = 0.004
    run(bot, w, clock, advance=20)
    assert bot.journal.open_copies("paper") == []
    assert "stop loss" in bot.journal.closed_copies("paper")[0]["reason"]


def test_drawdown_fires_the_kill_switch_and_sells_everything(world):
    bot, m, w, clock = world
    bot.cfg = dataclasses.replace(bot.cfg, exits=dataclasses.replace(bot.cfg.exits, stop_loss_pct=None))
    for i in (1, 2, 3):
        m.px[mint(i)] = 0.01
        run(bot, w, clock, trade(A, "buy", mint(i), 80, 0.01, clock.t))
    for i in (1, 2, 3):
        m.px[mint(i)] = 0.0001
    run(bot, w, clock, advance=20)
    assert bot.kill.tripped and bot.journal.open_copies("paper") == []
    m.px[mint(5)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(5), 40, 0.01, clock.t))
    assert bot.journal.open_copies("paper") == []


def test_no_rebuy_right_after_selling(world):
    bot, m, w, clock = world
    lose(bot, m, w, clock, A, 1)
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t))
    assert bot.journal.open_copies("paper") == []


def test_dashboard_state_has_what_the_page_reads(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(A, "buy", mint(1), 40, 0.01, clock.t), advance=20)
    s = bot.dashboard_state()
    for k in ("balance", "start", "pnl", "pnlPct", "multiple", "tradesCopied", "following", "fireRule", "equity", "stints",
              "lineup", "positions", "feed", "limits", "dailyStop", "killed", "mode"):
        assert k in s
    assert s["following"]["handle"] == "ace" and s["positions"][0]["handle"] == "ace"


def test_a_short_rpc_outage_does_not_fire_the_kill_switch(world):
    bot, m, w, clock = world

    def down():
        raise RuntimeError("rpc down")
    w.poll = down
    for _ in range(30):  # 30 seconds of errors, one step per second
        run(bot, w, clock)
    assert not bot.kill.tripped and bot.api_errors <= 2
    for _ in range(200):  # minutes of errors: now it fires
        run(bot, w, clock)
    assert bot.kill.tripped
