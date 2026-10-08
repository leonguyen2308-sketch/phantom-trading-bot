"""Deterministic fakes for tests and the kill-switch drill. Never used for real trading."""

from __future__ import annotations

import math

from phantom.features import Candle
from phantom.strategy import JevView


class FakeMarket:
    """A steady market with an optional breakout on the latest candle."""

    def __init__(self, n: int = 200, price: float = 60_000.0, breakout: bool = True, seconds: int = 900):
        self.seconds = seconds
        self.candles: list[Candle] = []
        t0 = 1_700_000_000
        prev = price
        for i in range(n):
            c = price + 500 * math.sin(i * 2 * math.pi / 24)  # a slow 6-hour swing: a wide range
            self.candles.append(Candle(t0 + i * seconds, prev, max(prev, c) + 40, min(prev, c) - 40, c, 10.0, seconds))
            prev = c
        if breakout:
            last = self.candles[-1]
            top = max(x.high for x in self.candles[-33:-1])
            self.candles[-1] = Candle(last.start, last.open, top + 360, min(last.open, last.low), top + 300, 30.0, seconds)
        self.now = self.candles[-1].end + 5

    def clock(self) -> float:
        return self.now

    def recent_candles(self, n: int = 200) -> list[Candle]:
        return [c for c in self.candles if c.end <= self.now][-n:]

    def ticker(self) -> float:
        return self.recent_candles()[-1].close

    def advance(self, close: float | None = None) -> None:
        last = self.candles[-1]
        c = close if close is not None else last.close
        self.candles.append(Candle(last.end, last.close, max(last.close, c) + 20, min(last.close, c) - 20, c, 10.0, self.seconds))
        self.now = self.candles[-1].end + 5


class FakeJev:
    def __init__(self, view: JevView):
        self.view, self.fail = view, False

    def ask(self, snap: dict) -> tuple[JevView, dict, float]:
        if self.fail:
            raise ConnectionError("fake Jev outage")
        return self.view, {"fake": True}, 1.0
