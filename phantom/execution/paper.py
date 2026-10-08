"""Paper broker: simulated swaps at the market price with fees and slippage. Balances persist across restarts."""

from __future__ import annotations

import json
import time
from pathlib import Path

from phantom.execution.base import Fill, Prepared


class PaperBroker:
    mode = "paper"

    def __init__(self, state_path: str | Path, start_usd: float = 1000.0, fee_bps: float = 10.0, slippage_bps: float = 5.0):
        self.path = Path(state_path)
        self.fee, self.slip = fee_bps / 1e4, slippage_bps / 1e4
        if self.path.exists():
            self.bal = json.loads(self.path.read_text())
        else:
            self.bal = {"usd": float(start_usd), "btc": 0.0, "sol": 1.0}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.bal))

    def balances(self) -> dict[str, float]:
        return dict(self.bal)

    def prepare_buy(self, usd: float, market_price: float) -> Prepared:
        px = market_price * (1 + self.slip)
        return Prepared("buy", usd, usd * (1 - self.fee) / px, px, self.slip * 1e4)

    def prepare_sell(self, btc: float, market_price: float) -> Prepared:
        px = market_price * (1 - self.slip)
        return Prepared("sell", btc, btc * px * (1 - self.fee), px, self.slip * 1e4)

    def execute(self, p: Prepared) -> Fill:
        ref = f"paper-{int(time.time() * 1000)}"
        if p.side == "buy":
            if p.in_amount > self.bal["usd"] + 1e-9:
                raise RuntimeError("not enough USD in the paper wallet")
            self.bal["usd"] -= p.in_amount
            self.bal["btc"] += p.expected_out
            fill = Fill("buy", p.expected_out, p.in_amount / p.expected_out, p.in_amount, ref)
        else:
            qty = min(p.in_amount, self.bal["btc"])
            self.bal["btc"] -= qty
            usd = qty * p.implied_price * (1 - self.fee)
            self.bal["usd"] += usd
            fill = Fill("sell", qty, usd / qty if qty else p.implied_price, usd, ref)
        self._save()
        return fill
