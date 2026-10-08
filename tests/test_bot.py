import pytest

from phantom.bot import Bot
from phantom.config import load_risk, load_strategy
from phantom.execution.paper import PaperBroker
from phantom.journal import Journal
from phantom.risk import KillSwitch
from phantom.sim import FakeJev, FakeMarket
from phantom.strategy import JevView

CFG = load_strategy("config/strategy.yaml")
LIMITS = load_risk("config/risk.yaml")
YES = JevView(0.8, "trending_up", 0.8, 0.8, 3.5, 0.02, "normal", 0.7)
NO = YES._replace(tp=0.2)


def make_bot(tmp_path, jev_view=YES, breakout=True, start_usd=1000.0):
    market = FakeMarket(breakout=breakout)
    bot = Bot(mode="paper", broker=PaperBroker(tmp_path / "paper.json", start_usd=start_usd, fee_bps=0, slippage_bps=0),
              market=market, jev=FakeJev(jev_view), cfg=CFG, limits=LIMITS, journal=Journal(tmp_path / "j.sqlite"),
              kill=KillSwitch(tmp_path / "kill.json"), clock=market.clock)
    return bot, market


def test_enters_when_every_check_passes(tmp_path):
    bot, market = make_bot(tmp_path)
    row = bot.on_candle()
    assert row["action"] == "enter", row["note"]
    assert bot.position() is not None
    assert bot.broker.balances()["btc"] > 0


def test_position_respects_hard_caps(tmp_path):
    bot, _ = make_bot(tmp_path, start_usd=100_000)
    bot.on_candle()
    pos = bot.position()
    assert pos["qty"] * pos["entry"] <= LIMITS.max_position_usd + 1e-6


def test_skips_when_jev_says_no(tmp_path):
    bot, _ = make_bot(tmp_path, jev_view=NO)
    row = bot.on_candle()
    assert row["action"] == "skip"
    assert bot.position() is None


def test_no_breakout_means_no_trade_even_if_jev_likes_it(tmp_path):
    bot, _ = make_bot(tmp_path, breakout=False)
    assert bot.on_candle()["action"] == "skip"


def test_stop_sells_the_position(tmp_path):
    bot, market = make_bot(tmp_path)
    bot.on_candle()
    stop = bot.position()["stop"]
    bot.check_exits(stop - 1)
    assert bot.position() is None
    assert bot.broker.balances()["btc"] == pytest.approx(0)
    assert bot.journal.closed_trades("paper")[0]["reason"] == "stop"


def test_drawdown_fires_kill_switch_and_flattens(tmp_path):
    bot, market = make_bot(tmp_path)
    bot.on_candle()
    bot.journal.put("peak:paper", 10_000.0)  # pretend equity used to be much higher
    market.advance()
    row = bot.on_candle()
    assert bot.kill.tripped
    assert bot.position() is None
    assert row["action"] == "halt"


def test_kill_switch_blocks_new_entries(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.kill.trip("manual test")
    assert bot.on_candle()["action"] == "halt"
    assert bot.position() is None


def test_api_errors_fire_kill_switch(tmp_path):
    bot, market = make_bot(tmp_path, jev_view=NO)
    bot.jev.fail = True
    for _ in range(LIMITS.max_consecutive_api_errors + 1):
        bot.on_candle()
        market.advance()
    assert bot.kill.tripped


def test_wallet_mismatch_fires_kill_switch(tmp_path):
    bot, market = make_bot(tmp_path)
    bot.on_candle()
    bot.broker.bal["btc"] = 0.0  # coins vanished from the wallet
    market.advance()
    bot.on_candle()
    assert bot.kill.tripped


def test_calibration_labels_resolve(tmp_path):
    bot, market = make_bot(tmp_path, jev_view=NO)
    bot.on_candle()
    for _ in range(CFG.time_stop_candles + 2):
        market.advance()
        bot.on_candle()
    p, y = bot.journal.calibration_samples(mode="paper", candidates_only=False)
    assert len(p) >= 1 and set(y) <= {0, 1}


def test_dashboard_state_shape(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.on_candle()
    s = bot.dashboard_state()
    for key in ("mode", "price", "equity", "peak", "dayStart", "pos", "tape", "eq", "closed", "calib", "lat", "killed"):
        assert key in s
    assert s["tape"][0]["action"] == "enter"


def test_kill_from_outside_flattens_on_next_price_check(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.on_candle()
    KillSwitch(tmp_path / "kill.json").trip("python -m phantom kill")
    bot.check_exits(bot.position()["entry"])
    assert bot.position() is None


# ---- review fixes ----
from phantom.execution.jupiter import SwapOutcomeUnknown  # noqa: E402


class LandsAnyway(PaperBroker):
    """Swap lands, but the API call raises (timeout after broadcast)."""
    def execute(self, p):
        super().execute(p)
        raise SwapOutcomeUnknown("timed out")


def test_buy_that_errors_but_lands_is_tracked(tmp_path):
    market = FakeMarket()
    bot = Bot(mode="paper", broker=LandsAnyway(tmp_path / "p.json", 1000, 0, 0), market=market, jev=FakeJev(YES), cfg=CFG,
              limits=LIMITS, journal=Journal(tmp_path / "j.sqlite"), kill=KillSwitch(tmp_path / "k.json"), clock=market.clock)
    assert bot.on_candle()["action"] == "enter"
    assert bot.position()["qty"] == pytest.approx(bot.broker.balances()["btc"])


def test_untracked_btc_trips_kill_switch_and_is_sold(tmp_path):
    bot, market = make_bot(tmp_path, jev_view=NO)
    bot.on_candle()
    bot.broker.bal["btc"] = 0.01  # BTC the bot is not tracking
    market.advance()
    bot.on_candle()
    assert bot.kill.tripped and bot.broker.balances()["btc"] == pytest.approx(0)


def test_exit_sells_what_the_wallet_holds_when_short(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.on_candle()
    bot.broker.bal["btc"] *= 0.995  # tiny shortfall (fees, rounding)
    bot.check_exits(bot.position()["stop"] - 1)
    assert bot.position() is None and bot.broker.balances()["btc"] == pytest.approx(0)


def test_deposit_while_flat_is_not_profit_and_withdrawal_is_not_drawdown(tmp_path):
    bot, market = make_bot(tmp_path, jev_view=NO)
    bot.on_candle()
    bot.broker.bal["usd"] -= 500  # withdraw half
    market.advance()
    bot.on_candle()
    assert not bot.kill.tripped
    assert bot.journal.get("peak:paper") == pytest.approx(500)


def test_wallet_read_error_does_not_lose_the_candle(tmp_path):
    bot, market = make_bot(tmp_path, jev_view=NO)
    real = bot.broker.balances
    bot.broker.balances = lambda: (_ for _ in ()).throw(RuntimeError("rpc down"))
    assert bot.on_candle() is None
    bot.broker.balances = real
    assert bot.on_candle() is not None  # same candle processed once the RPC is back
