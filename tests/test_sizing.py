import pytest

from phantom.config import RiskLimits
from phantom.sizing import KellyConfig, kelly_fraction, kelly_multiplier, position_size

LIMITS = RiskLimits(
    max_position_pct=0.20, max_position_usd=100.0, max_risk_per_trade_pct=0.005,
    daily_loss_limit_pct=0.02, max_drawdown_pct=0.10, max_consecutive_api_errors=5,
    stale_data_candles=2, max_price_deviation_bps=75, max_price_impact_bps=50, min_sol_for_fees=0.02,
)
KELLY = KellyConfig(p_cut=0.58, p_full=0.72, max_multiplier=0.25)


def test_kelly_fraction_formula():
    assert kelly_fraction(0.6, 1.5) == pytest.approx(0.6 - 0.4 / 1.5)
    assert kelly_fraction(0.3, 1.5) < 0


def test_multiplier_is_zero_below_cutoff_and_capped_at_quarter():
    assert kelly_multiplier(0.57, KELLY) == 0
    assert kelly_multiplier(0.65, KELLY) == pytest.approx(0.125)
    assert kelly_multiplier(0.95, KELLY) == 0.25


def test_fixed_risk_when_kelly_disabled():
    s = position_size(equity=1000, entry=100, stop=98, p=0.9, b=1.5, kelly=KELLY, limits=LIMITS, kelly_enabled=False, fixed_risk_pct=0.002)
    assert s.risk_pct == 0.002
    assert s.qty == pytest.approx(1000 * 0.002 / 2)


def test_kelly_risk_never_exceeds_per_trade_limit():
    s = position_size(equity=10_000, entry=100, stop=99, p=0.9, b=1.5, kelly=KELLY, limits=LIMITS, kelly_enabled=True, fixed_risk_pct=0.002)
    assert s.risk_pct <= LIMITS.max_risk_per_trade_pct


def test_notional_capped_by_percent_and_absolute_usd():
    s = position_size(equity=10_000, entry=100, stop=99.9, p=0.7, b=1.5, kelly=KELLY, limits=LIMITS, kelly_enabled=False, fixed_risk_pct=0.004)
    assert s.qty * 100 <= 100.0 + 1e-9  # absolute $100 cap beats 20% of $10k
    assert "max_position_usd" in s.capped_by


def test_zero_size_below_cutoff_when_kelly_enabled():
    s = position_size(equity=1000, entry=100, stop=98, p=0.55, b=1.5, kelly=KELLY, limits=LIMITS, kelly_enabled=True, fixed_risk_pct=0.002)
    assert s.qty == 0


def test_fixed_risk_above_limit_is_rejected():
    with pytest.raises(ValueError):
        position_size(equity=1000, entry=100, stop=98, p=0.7, b=1.5, kelly=KELLY, limits=LIMITS, kelly_enabled=False, fixed_risk_pct=0.01)
