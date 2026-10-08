import random

import pytest

from phantom.calibration import brier, calibration_gate, ece, fit_isotonic, reliability


def test_brier_perfect_and_worst():
    assert brier([1.0, 0.0], [1, 0]) == 0
    assert brier([0.0, 1.0], [1, 0]) == 1


def test_reliability_bins_counts():
    bins = reliability([0.05, 0.15, 0.15, 0.95], [0, 0, 1, 1], n_bins=10)
    assert sum(b.n for b in bins) == 4
    assert next(b for b in bins if b.lo == pytest.approx(0.1)).actual == 0.5


def test_isotonic_fixes_overconfidence():
    rng = random.Random(1)
    p = [rng.uniform(0.5, 0.9) for _ in range(3000)]
    y = [1 if rng.random() < q - 0.1 else 0 for q in p]  # model is 10 points overconfident
    f = fit_isotonic(p, y)
    assert ece([f(q) for q in p], y) < ece(p, y)
    assert all(f(a) <= f(b) for a, b in zip(sorted(p), sorted(p)[1:]))  # monotone


def test_gate_needs_enough_samples_and_low_ece():
    ok, _ = calibration_gate([0.6] * 50, [1] * 30 + [0] * 20, min_samples=200, max_ece=0.05)
    assert not ok
    rng = random.Random(2)
    p = [rng.uniform(0.5, 0.9) for _ in range(400)]
    y = [1 if rng.random() < q else 0 for q in p]
    ok, detail = calibration_gate(p, y, min_samples=200, max_ece=0.08)
    assert ok, detail
