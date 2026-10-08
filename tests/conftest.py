import math
import random

import pytest

from phantom.features import Candle


def make_candles(n=400, start=1_700_000_000, seconds=900, seed=7, drift=0.0, vol=0.003, price=60_000.0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        o = price
        c = o * math.exp(drift + rng.gauss(0, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        out.append(Candle(start=start + i * seconds, open=o, high=h, low=lo, close=c, volume=10 + rng.random() * 5, seconds=seconds))
        price = c
    return out


@pytest.fixture
def candles():
    return make_candles()
