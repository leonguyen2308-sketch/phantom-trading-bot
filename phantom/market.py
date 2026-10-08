"""BTC-USD market data from Coinbase's public API (no key needed). Only closed candles are returned."""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from phantom.features import Candle

BASE = "https://api.exchange.coinbase.com"
MAX_PER_REQUEST = 300


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoinbaseMarket:
    def __init__(self, product: str = "BTC-USD", granularity: int = 900, http: httpx.Client | None = None, clock=time.time):
        self.product, self.g, self.clock = product, granularity, clock
        self.http = http or httpx.Client(timeout=15, headers={"User-Agent": "phantom-trading-bot"})

    def _fetch(self, start: int, end: int) -> list[Candle]:
        r = self.http.get(f"{BASE}/products/{self.product}/candles",
                          params={"granularity": self.g, "start": _iso(start), "end": _iso(end)})
        r.raise_for_status()
        # rows: [time, low, high, open, close, volume], newest first
        return [Candle(int(t), float(o), float(h), float(lo), float(c), float(v), self.g) for t, lo, h, o, c, v in r.json()]

    def recent_candles(self, n: int = 200) -> list[Candle]:
        now = int(self.clock())
        out: dict[int, Candle] = {}
        end = now
        while len(out) < n + 1:
            start = end - MAX_PER_REQUEST * self.g
            batch = self._fetch(start, end)
            if not batch:
                break
            out.update({c.start: c for c in batch})
            end = start
        closed = sorted((c for c in out.values() if c.end <= now), key=lambda c: c.start)
        return closed[-n:]

    def ticker(self) -> float:
        r = self.http.get(f"{BASE}/products/{self.product}/ticker")
        r.raise_for_status()
        return float(r.json()["price"])

    def history(self, days: int, cache: str | Path | None = "data/btc_usd_15m.csv", progress=None) -> list[Candle]:
        """Download `days` of closed candles, reusing and extending a CSV cache."""
        have: dict[int, Candle] = {c.start: c for c in load_csv(cache)} if cache and Path(cache).exists() else {}
        now = int(self.clock())
        start_all = now - days * 86_400
        end = now
        while end > start_all:
            start = max(start_all, end - MAX_PER_REQUEST * self.g)
            if not all(t in have for t in range(start - start % self.g + self.g, end - self.g, self.g)):
                for c in self._fetch(start, end):
                    have[c.start] = c
                time.sleep(0.12)  # stay under the public rate limit
            end = start
            if progress:
                progress((now - end) / (now - start_all))
        candles = sorted((c for c in have.values() if c.end <= now and c.start >= start_all), key=lambda c: c.start)
        if cache:
            save_csv(cache, sorted(have.values(), key=lambda c: c.start))
        return candles


def save_csv(path: str | Path, candles: list[Candle]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "open", "high", "low", "close", "volume", "seconds"])
        for c in candles:
            w.writerow([c.start, c.open, c.high, c.low, c.close, c.volume, c.seconds])


def load_csv(path: str | Path) -> list[Candle]:
    with open(path) as f:
        r = csv.DictReader(f)
        return [Candle(int(x["start"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                       float(x["volume"]), int(x["seconds"])) for x in r]
