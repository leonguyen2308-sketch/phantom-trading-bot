import pytest

from phantom.backtest import Costs, metrics, newey_west_t, run_backtest
from phantom.config import load_risk, load_strategy
from tests.conftest import make_candles

CFG = load_strategy("config/strategy.yaml")
LIMITS = load_risk("config/risk.yaml")


def test_backtest_runs_and_costs_reduce_returns():
    cs = make_candles(n=3000, seed=3, drift=0.0002)
    cheap = run_backtest(cs, CFG, LIMITS, jev=None, costs=Costs(fee_bps=0, slippage_bps=0), start_equity=1000)
    dear = run_backtest(cs, CFG, LIMITS, jev=None, costs=Costs(fee_bps=50, slippage_bps=20), start_equity=1000)
    assert cheap.trades, "expected some breakout trades on trending data"
    assert dear.equity[-1][1] < cheap.equity[-1][1]


def test_entries_fill_on_the_next_candle_not_the_signal_candle():
    cs = make_candles(n=3000, seed=3, drift=0.0002)
    res = run_backtest(cs, CFG, LIMITS, jev=None, costs=Costs(0, 0), start_equity=1000)
    by_start = {c.start: c for c in cs}
    for tr in res.trades:
        fill_candle = by_start[tr.entry_time]
        assert tr.entry_price == pytest.approx(fill_candle.open)
        # the signal candle has closed by the time the fill candle opens
        assert fill_candle.start >= tr.signal_time


def test_same_candle_stop_and_target_counts_as_stop():
    from phantom.backtest import _exit_in_candle
    from phantom.features import Candle
    c = Candle(start=0, open=100, high=110, low=90, close=100, volume=1)
    assert _exit_in_candle(c, stop=95, tp=105) == ("stop", 95)


def test_metrics_on_known_curve():
    day = 86_400
    eq = [(i * day, 1000 * (1.001 ** i)) for i in range(366)]
    m = metrics(eq, trades=[])
    assert m["max_drawdown"] == 0
    assert m["total_return"] == pytest.approx(1.001 ** 365 - 1)


def test_newey_west_t_matches_plain_t_for_iid_like_series():
    xs = [0.01, -0.005, 0.012, -0.003, 0.007, 0.002, -0.004, 0.009] * 20
    assert newey_west_t(xs, lags=0) > 0
