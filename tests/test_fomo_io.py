import base64

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from fomo.brokers import LiveBroker, PaperBroker
from fomo.chain import RpcError
from fomo.config import load_config
from fomo.leaderboard import Trader
from fomo.research import replay, trader_stats
from fomo.swaps import USDC, Swap
from fomo.watcher import LeaderWatcher, usdc_account
from tests.test_fomo_swaps import tb, tx

MEME = "MemeMint11111111111111111111111111111111111"


class FakeRpc:
    def __init__(self):
        self.sigs: dict[str, list[dict]] = {}
        self.txs: dict[str, dict | None] = {}
        self.fail = False

    def signatures(self, address, limit=50, before=None, until=None):
        if self.fail:
            raise RpcError("down")
        out = []
        for s in self.sigs.get(address, []):  # newest first
            if s["signature"] == until:
                break
            out.append(s)
        return out[:limit]

    def transaction(self, sig):
        return self.txs.get(sig)


def buy_tx(wallet, t):
    d = tx([tb(1, USDC, wallet, 10**8, 6)], [tb(1, USDC, wallet, 0, 6), tb(2, MEME, wallet, 10**6, 6)], keys=(wallet,))
    d["blockTime"] = t
    return d


def test_watcher_starts_from_now_and_reports_new_trades_once():
    w = str(Keypair().pubkey())
    rpc, now = FakeRpc(), [1_700_000_000]
    rpc.sigs[w] = [{"signature": "old", "err": None}]
    rpc.txs["old"] = buy_tx(w, now[0] - 5)
    watcher = LeaderWatcher(rpc, lambda: 150.0, use_ws=False, fallback_poll_seconds=0, clock=lambda: now[0])
    watcher.watch({w})
    assert watcher.poll() == []  # the trade from before we started watching is never copied
    rpc.sigs[w].insert(0, {"signature": "new", "err": None})
    rpc.txs["new"] = buy_tx(w, now[0])
    got = watcher.poll()
    assert [s.sig for s in got] == ["sig1"] and got[0].side == "buy" and got[0].usd == pytest.approx(100)
    assert watcher.poll() == []


def test_watcher_retries_a_transaction_the_rpc_hasnt_indexed_yet_and_drops_stale_ones():
    w = str(Keypair().pubkey())
    rpc, now = FakeRpc(), [1_700_000_000]
    watcher = LeaderWatcher(rpc, lambda: 150.0, use_ws=False, fallback_poll_seconds=0, clock=lambda: now[0], max_age_seconds=60)
    watcher.watch({w})
    watcher.inbox.put("late")
    assert watcher.poll() == [] and "late" in watcher.retry
    rpc.txs["late"] = buy_tx(w, now[0] - 1)
    assert len(watcher.poll()) == 1
    watcher.inbox.put("stale")
    rpc.txs["stale"] = buy_tx(w, now[0] - 600)
    rpc.txs["stale"]["transaction"]["signatures"] = ["stale"]
    assert watcher.poll() == []


def test_watcher_also_watches_the_traders_usdc_account():
    w = str(Keypair().pubkey())
    watcher = LeaderWatcher(FakeRpc(), lambda: 150.0, use_ws=False)
    watcher.watch({w})
    assert set(watcher.addr_to_wallet) == {w, usdc_account(w)}


def test_watcher_raises_when_the_rpc_is_down_so_the_bot_counts_it():
    rpc = FakeRpc()
    watcher = LeaderWatcher(rpc, lambda: 150.0, use_ws=False, fallback_poll_seconds=0)
    watcher.watch({str(Keypair().pubkey())})
    rpc.fail = True
    with pytest.raises(RpcError):
        watcher.poll()


# ---- research replay ----
def sw(i, wallet, side, price, t, pre=0, usd=400.0):
    raw = int(usd / price * 1e6) if side == "buy" else pre
    return Swap(f"r{i}", i, t, wallet, side, f"M{i // 2}".ljust(44, "x"), raw, 6, usd if side == "buy" else raw / 1e6 * price,
                "USDC", pre, raw if side == "buy" else 0)


def test_replay_applies_the_fire_rule_on_history():
    cfg = load_config("tests/fomo_classic.yaml")
    a, b = "A" * 44, "B" * 44
    h = {a: [], b: []}
    t = 1000
    for k in range(2):  # ace loses twice
        buy = sw(4 * k, a, "buy", 1.0, t)
        h[a] += [buy, sw(4 * k + 1, a, "sell", 0.5, t + 10, pre=buy.raw)]
        t += 100
    buy = sw(20, b, "buy", 1.0, t)
    h[b] += [buy, sw(21, b, "sell", 3.0, t + 10, pre=buy.raw)]
    r = replay([Trader("ace", a, 1), Trader("bee", b, 2)], h, cfg, slip=0.0, fee=0.0)
    assert len(r["fires"]) == 1 and r["closed"] == 3
    # copies are capped at 20% of equity: $20 (-50%), then $18 (-50%), then $16.20 (+200%)
    assert r["pnl"] == pytest.approx(-10 - 9 + 32.4)
    worse = replay([Trader("ace", a, 1), Trader("bee", b, 2)], h, cfg, slip=0.08)
    assert worse["pnl"] < r["pnl"]


def test_replay_in_crowd_mode_copies_everyone_and_fires_each_trader_on_their_own():
    cfg = load_config("config/fomo.yaml")  # follow_all, 3 losses to fire, $5 a copy
    a, b, c = "A" * 44, "B" * 44, "C" * 44
    h = {a: [], b: [], c: []}
    t = 1000
    for k in range(3):  # ace loses three times, and is fired after the third
        buy = sw(4 * k, a, "buy", 1.0, t)
        h[a] += [buy, sw(4 * k + 1, a, "sell", 0.5, t + 10, pre=buy.raw)]
        t += 100
    buy = sw(30, b, "buy", 1.0, t)
    h[b] += [buy, sw(31, b, "sell", 3.0, t + 10, pre=buy.raw)]  # bee wins
    t += 100
    buy = sw(40, c, "buy", 1.0, t)  # cat buys the coin that is still open from nobody else: a separate copy
    h[c] += [buy, sw(41, c, "sell", 2.0, t + 10, pre=buy.raw)]
    t += 100
    buy = sw(50, a, "buy", 1.0, t)  # ace is fired now: this buy is not copied
    h[a] += [buy, sw(51, a, "sell", 0.5, t + 10, pre=buy.raw)]
    r = replay([Trader("ace", a, 1), Trader("bee", b, 2), Trader("cat", c, 3)], h, cfg, slip=0.0, fee=0.0)
    assert len(r["fires"]) == 1 and r["closed"] == 5 and r["wins"] == 2  # 3 ace losses + bee + cat; ace's fourth buy was not taken
    assert r["pnl"] == pytest.approx(-2.5 * 3 + 10 + 5)  # $5 copies: -50% x3, +200%, +100%


def test_trader_stats_round_trips():
    a = "A" * 44
    buy = sw(0, a, "buy", 1.0, 0)
    s = trader_stats([buy, sw(1, a, "sell", 2.0, 3600, pre=buy.raw)])
    assert s["round_trips"] == 1 and s["win_rate"] == 1.0 and s["realized_usd"] == pytest.approx(400)


# ---- brokers ----
def order(input_mint, output_mint, amount, out, tx_b64=None):
    return {"inputMint": input_mint, "outputMint": output_mint, "inAmount": str(amount), "outAmount": str(out),
            "otherAmountThreshold": str(int(out * 0.95)), "priceImpact": "0.4", "transaction": tx_b64, "requestId": "req"}


def test_paper_broker_uses_real_quotes_plus_slippage(tmp_path):
    def handler(req):
        p = dict(req.url.params)
        assert "taker" not in p and req.headers["x-api-key"] == "k"
        if p["inputMint"] == USDC:
            return httpx.Response(200, json=order(USDC, MEME, int(p["amount"]), 1_000_000))
        return httpx.Response(200, json=order(MEME, USDC, int(p["amount"]), 12_000_000))
    b = PaperBroker(tmp_path / "p.json", "k", start_usd=100, slippage_bps=100, http=httpx.Client(transport=httpx.MockTransport(handler)))
    f = b.execute(b.quote_buy(MEME, 10))
    assert b.usdc() == pytest.approx(90) and f.raw == 990_000
    b.execute(b.quote_sell(MEME, f.raw))
    assert b.usdc() == pytest.approx(90 + 12 * 0.99) and b.token_raw(MEME) == 0
    assert PaperBroker(tmp_path / "p.json", "k").usdc() == pytest.approx(b.usdc())  # survives a restart


def test_paper_broker_reports_no_route(tmp_path):
    def handler(req):
        return httpx.Response(400, json={"error": "No routes found"})
    b = PaperBroker(tmp_path / "p.json", "k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="no route"):
        b.quote_buy(MEME, 10)


def test_live_broker_signs_inspected_order_and_reads_the_real_fill():
    kp = Keypair()
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    unsigned = base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()

    def handler(req):
        if req.url.path.endswith("/order"):
            assert dict(req.url.params)["taker"] == str(kp.pubkey())
            return httpx.Response(200, json=order(USDC, MEME, int(dict(req.url.params)["amount"]), 5_000_000, unsigned))
        return httpx.Response(200, json={"status": "Success", "signature": "SIG", "totalInputAmount": "20000000", "totalOutputAmount": "4900000"})
    b = LiveBroker(kp, "k", rpc=None, http=httpx.Client(transport=httpx.MockTransport(handler)), poll_seconds=0.1, sleep=lambda s: None)
    q = b.quote_buy(MEME, 20)
    assert q.raw == 5_000_000 and q.impact_pct == pytest.approx(0.4)
    f = b.execute(q)
    assert (f.raw, f.usd, f.ref) == (4_900_000, 20.0, "SIG")


def test_research_reads_trades_that_only_name_the_traders_usdc_account(tmp_path):
    from fomo.research import SwapCache, fetch_swaps
    w = str(Keypair().pubkey())
    fomo_signer = str(Keypair().pubkey())
    rpc = FakeRpc()
    t = 1_700_000_000
    d = tx([tb(1, USDC, w, 10**8, 6)], [tb(1, USDC, w, 0, 6), tb(2, MEME, w, 10**6, 6)], keys=(fomo_signer,))
    d["blockTime"] = t
    rpc.sigs[usdc_account(w)] = [{"signature": "s", "err": None, "blockTime": t}]
    rpc.txs["s"] = d
    got = fetch_swaps(rpc, w, t - 10, 150.0, 50, SwapCache(tmp_path / "c.sqlite"))
    assert len(got) == 1 and got[0].side == "buy" and got[0].wallet == w


def test_demo_world_runs_hours_without_errors(tmp_path):
    from fomo.bot import CopyBot
    from fomo.journal import Journal
    from fomo.sim import SimBroker, SimDex, SimPrices, SimWatcher, SimWorld
    from phantom.risk import KillSwitch
    world = SimWorld(seed=1, speed=1)
    now = [world.t0]
    world.clock = lambda: now[0]
    dex = SimDex(world)
    bot = CopyBot(mode="demo", cfg=load_config("config/fomo.yaml"), broker=SimBroker(world), watcher=SimWatcher(world),
                  prices=SimPrices(world), dex=dex, mint_info=dex.mint_info, lineup=world.lineup,
                  journal=Journal(tmp_path / "d.sqlite"), kill=KillSwitch(tmp_path / "k.json"), clock=world.clock)
    errors = []
    bot._api_error = errors.append
    for _ in range(4 * 3600 // 10):
        now[0] += 10
        bot.step()
    assert errors == []
    assert bot.journal.count_copies("demo") > 0
    s = bot.dashboard_state()
    assert s["balance"] > 0 and len(s["equity"]) > 10


def test_crowd_demo_follows_forty_made_up_traders_for_hours_without_errors(tmp_path):
    from fomo.bot import CopyBot
    from fomo.journal import Journal
    from fomo.sim import SimBroker, SimDex, SimPrices, SimWatcher, SimWorld
    from phantom.risk import KillSwitch
    world = SimWorld(seed=3, speed=1, n_traders=40)
    now = [world.t0]
    world.clock = lambda: now[0]
    dex = SimDex(world)
    bot = CopyBot(mode="demo", cfg=load_config("config/fomo.yaml"), broker=SimBroker(world), watcher=SimWatcher(world),
                  prices=SimPrices(world), dex=dex, mint_info=dex.mint_info, lineup=world.lineup,
                  journal=Journal(tmp_path / "d.sqlite"), kill=KillSwitch(tmp_path / "k.json"), clock=world.clock)
    errors = []
    bot._api_error = errors.append
    for _ in range(6 * 3600 // 10):
        now[0] += 10
        bot.step()
    s = bot.dashboard_state()
    assert errors == [] and s["crowd"]["followed"] + s["crowd"]["fired"] == 40
    assert bot.journal.count_copies("demo") > 10 and len({c["trader"] for c in bot.journal.closed_copies("demo")}) > 3
    assert s["balance"] > 0 and len(s["positions"]) <= bot.cfg.limits.max_open_positions
