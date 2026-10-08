from phantom.config import load_strategy
from phantom.strategy import JevView, evaluate, exit_levels, is_breakout

CFG = load_strategy("config/strategy.yaml")

GOOD = JevView(regime_up=0.7, regime_top="trending_up", dir_up=0.7, pressure=0.75, quality=3.0, extreme=0.05, risk_top="normal", tp=0.66)


def test_all_thresholds_pass_fires():
    ev = evaluate(GOOD, CFG)
    assert ev.fire, ev.failures


def test_each_failing_question_blocks_the_trade():
    for field, bad in [("regime_up", 0.1), ("dir_up", 0.2), ("pressure", 0.3), ("quality", 1.0), ("extreme", 0.5), ("tp", 0.4)]:
        ev = evaluate(GOOD._replace(**{field: bad}), CFG)
        assert not ev.fire, field
        assert ev.failures


def test_exit_levels_use_atr_multiples():
    stop, tp = exit_levels(100.0, 2.0, CFG)
    assert stop == 100 - CFG.stop_atr * 2
    assert tp == 100 + CFG.take_profit_atr * 2


def test_breakout_rule():
    snap = {"dist_high_atr": 0.3, "range_width_atr": 3.0, "rv_ratio": 1.0}
    assert is_breakout(snap, CFG)
    assert not is_breakout({**snap, "dist_high_atr": -0.5}, CFG)
