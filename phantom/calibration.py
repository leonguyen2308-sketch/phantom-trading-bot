"""Calibration: Brier score, reliability curve, ECE, isotonic recalibration and the sizing gate."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Callable


def brier(p: list[float], y: list[int]) -> float:
    return sum((a - b) ** 2 for a, b in zip(p, y)) / len(p)


def base_rate_brier(y: list[int]) -> float:
    r = sum(y) / len(y)
    return r * (1 - r)


@dataclass
class Bin:
    lo: float
    hi: float
    n: int
    predicted: float
    actual: float


def reliability(p: list[float], y: list[int], n_bins: int = 10) -> list[Bin]:
    bins = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        idx = [k for k, q in enumerate(p) if lo <= q < hi or (i == n_bins - 1 and q == 1.0)]
        if idx:
            bins.append(Bin(lo, hi, len(idx), sum(p[k] for k in idx) / len(idx), sum(y[k] for k in idx) / len(idx)))
    return bins


def ece(p: list[float], y: list[int], n_bins: int = 10) -> float:
    return sum(b.n * abs(b.predicted - b.actual) for b in reliability(p, y, n_bins)) / len(p)


def fit_isotonic(p: list[float], y: list[int]) -> Callable[[float], float]:
    """Pool-adjacent-violators. Returns a monotone step map from raw to calibrated probability."""
    pairs = sorted(zip(p, y))
    blocks: list[list[float]] = []  # [sum_y, count, max_x]
    for x, t in pairs:
        blocks.append([float(t), 1.0, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, n, mx = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += n
            blocks[-1][2] = mx
    xs = [b[2] for b in blocks]
    vs = [b[0] / b[1] for b in blocks]

    def f(q: float) -> float:
        i = bisect.bisect_left(xs, q)
        return vs[min(i, len(vs) - 1)]

    return f


def calibration_gate(p: list[float], y: list[int], min_samples: int, max_ece: float) -> tuple[bool, str]:
    n = len(p)
    if n < min_samples:
        return False, f"{n} of {min_samples} resolved decisions"
    e = ece(p, y)
    if e > max_ece:
        return False, f"ECE {e:.3f} is above {max_ece}"
    return True, f"{n} decisions, ECE {e:.3f}"
