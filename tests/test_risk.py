from phantom.risk import AccountState, KillSwitch, kill_trigger, pre_trade_vetoes
from tests.test_sizing import LIMITS


def healthy(**kw):
    base = dict(equity=1000.0, peak_equity=1000.0, day_start_equity=1000.0, open_notional=0.0,
                consecutive_api_errors=0, candles_since_data=0, position_mismatch=False)
    base.update(kw)
    return AccountState(**base)


def test_healthy_account_has_no_trigger_or_veto(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    assert kill_trigger(healthy(), LIMITS) is None
    assert pre_trade_vetoes(50.0, healthy(), LIMITS, ks, price_deviation_bps=5, price_impact_bps=5) == []


def test_drawdown_trips_kill_switch():
    assert "drawdown" in kill_trigger(healthy(equity=899, peak_equity=1000), LIMITS)


def test_api_errors_trip_kill_switch():
    assert "API errors" in kill_trigger(healthy(consecutive_api_errors=5), LIMITS)


def test_position_mismatch_trips_kill_switch():
    assert "position" in kill_trigger(healthy(position_mismatch=True), LIMITS)


def test_kill_switch_persists_and_needs_manual_reset(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    ks.trip("test")
    assert KillSwitch(tmp_path / "kill.json").tripped  # survives restart
    assert pre_trade_vetoes(10.0, healthy(), LIMITS, ks, 0, 0)
    ks.reset(confirm="RESET")
    assert not KillSwitch(tmp_path / "kill.json").tripped


def test_reset_without_confirmation_is_refused(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    ks.trip("test")
    try:
        ks.reset(confirm="yes")
    except PermissionError:
        pass
    assert ks.tripped


def test_daily_loss_blocks_new_entries(tmp_path):
    v = pre_trade_vetoes(10.0, healthy(equity=979, day_start_equity=1000, peak_equity=1000), LIMITS, KillSwitch(tmp_path / "k.json"), 0, 0)
    assert any("daily loss" in x for x in v)


def test_stale_data_blocks_new_entries(tmp_path):
    v = pre_trade_vetoes(10.0, healthy(candles_since_data=2), LIMITS, KillSwitch(tmp_path / "k.json"), 0, 0)
    assert any("stale" in x for x in v)


def test_oversize_order_is_vetoed(tmp_path):
    v = pre_trade_vetoes(150.0, healthy(), LIMITS, KillSwitch(tmp_path / "k.json"), 0, 0)
    assert any("position" in x for x in v)


def test_bad_quote_is_vetoed(tmp_path):
    ks = KillSwitch(tmp_path / "k.json")
    assert any("price" in x for x in pre_trade_vetoes(10.0, healthy(), LIMITS, ks, price_deviation_bps=100, price_impact_bps=0))
    assert any("impact" in x for x in pre_trade_vetoes(10.0, healthy(), LIMITS, ks, price_deviation_bps=0, price_impact_bps=80))
