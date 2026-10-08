"""Hard limits and the kill switch. Checked before every order. No model can override these."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from phantom.config import RiskLimits


@dataclass
class AccountState:
    equity: float
    peak_equity: float
    day_start_equity: float
    open_notional: float
    consecutive_api_errors: int
    candles_since_data: int
    position_mismatch: bool


class KillSwitch:
    """File-backed so it survives restarts. Only a human reset (typed confirmation) clears it."""

    def __init__(self, path: str | Path = "state/kill.json"):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {"tripped": True, "reason": "kill switch file is unreadable"}
        except FileNotFoundError:
            return {"tripped": False}
        except (OSError, ValueError):  # corrupt or half-written: fail closed
            return {"tripped": True, "reason": "kill switch file is unreadable"}

    @property
    def tripped(self) -> bool:
        return bool(self._read().get("tripped"))

    @property
    def reason(self) -> str:
        return self._read().get("reason", "")

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)  # atomic: never leaves a half-written file

    def trip(self, reason: str) -> None:
        self._write({"tripped": True, "reason": reason, "at": int(time.time())})

    def reset(self, confirm: str) -> None:
        if confirm != "RESET":
            raise PermissionError("Kill switch reset needs the typed confirmation RESET")
        self._write({"tripped": False, "reset_at": int(time.time())})


def kill_trigger(s: AccountState, limits: RiskLimits) -> str | None:
    """Return why the kill switch must fire now, or None."""
    if s.peak_equity > 0 and (s.peak_equity - s.equity) / s.peak_equity >= limits.max_drawdown_pct:
        return f"Max drawdown {limits.max_drawdown_pct:.0%} reached"
    if s.consecutive_api_errors >= limits.max_consecutive_api_errors:
        return f"{s.consecutive_api_errors} API errors in a row"
    if s.position_mismatch:
        return "Bot position does not match the wallet balance"
    return None


def pre_trade_vetoes(order_notional: float, s: AccountState, limits: RiskLimits, kill: KillSwitch,
                     price_deviation_bps: float, price_impact_bps: float) -> list[str]:
    """Every reason this entry must not be sent. Empty list means it may go."""
    v: list[str] = []
    if kill.tripped:
        v.append("kill switch is on")
    reason = kill_trigger(s, limits)
    if reason:
        v.append(reason)
    if s.day_start_equity > 0 and (s.day_start_equity - s.equity) / s.day_start_equity >= limits.daily_loss_limit_pct:
        v.append("daily loss limit reached; no new entries until 00:00 UTC")
    if s.candles_since_data >= limits.stale_data_candles:
        v.append("market data is stale")
    cap = min(s.equity * limits.max_position_pct, limits.max_position_usd)
    if s.open_notional + order_notional > cap + 1e-9:
        v.append(f"position would exceed the cap of ${cap:,.2f}")
    if price_deviation_bps > limits.max_price_deviation_bps:
        v.append(f"swap price is {price_deviation_bps:.0f} bps away from the market price")
    if price_impact_bps > limits.max_price_impact_bps:
        v.append(f"price impact {price_impact_bps:.0f} bps is too high")
    return v
