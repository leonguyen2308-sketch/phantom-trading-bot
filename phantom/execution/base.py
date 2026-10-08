"""What every broker (paper or Jupiter) offers the bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Prepared:
    side: str  # "buy" or "sell"
    in_amount: float  # USD for a buy, BTC for a sell
    expected_out: float  # BTC for a buy, USD for a sell
    implied_price: float  # USD per BTC
    price_impact_bps: float
    payload: Any = None


@dataclass
class Fill:
    side: str
    qty: float  # BTC
    price: float  # effective USD per BTC, after costs
    usd: float
    ref: str  # paper id or Solana signature


class Broker(Protocol):
    mode: str

    def balances(self) -> dict[str, float]: ...  # {"usd":..., "btc":..., "sol":...}

    def prepare_buy(self, usd: float, market_price: float) -> Prepared: ...

    def prepare_sell(self, btc: float, market_price: float) -> Prepared: ...

    def execute(self, prepared: Prepared) -> Fill: ...
