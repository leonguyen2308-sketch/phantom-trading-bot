"""Settings, limits and secret handling. Secrets come only from the environment or .env."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks and comments; strips matching quotes."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_env(path: str | Path = ".env") -> None:
    """Load .env into os.environ without overriding variables already set."""
    for k, v in load_env_file(path).items():
        os.environ.setdefault(k, v)


SECRET_ENV_KEYS = ("TYPESAFE_API_KEY", "JUPITER_API_KEY", "PHANTOM_BOT_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "SOLANA_RPC_URL",
                  "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


class _Redact(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def _scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self._scrub(record.getMessage()), None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


def install_redaction(extra: list[str] | None = None) -> None:
    """Scrub every known secret value from every log record, on every handler."""
    secrets = [os.environ.get(k, "") for k in SECRET_ENV_KEYS] + list(extra or [])
    f = _Redact(secrets)
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    logging.setLogRecordFactory(_factory_with(f, logging.getLogRecordFactory()))


def _factory_with(f: _Redact, base):
    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        f.filter(record)
        return record
    return factory


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float
    max_position_usd: float
    max_risk_per_trade_pct: float
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    max_consecutive_api_errors: int
    stale_data_candles: int
    max_price_deviation_bps: float
    max_price_impact_bps: float
    min_sol_for_fees: float


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_risk(path: str | Path = "config/risk.yaml") -> RiskLimits:
    data = yaml.safe_load(Path(path).read_text())
    return RiskLimits(**data)


@dataclass(frozen=True)
class StrategyConfig:
    version: str
    timeframe_minutes: int
    breakout_lookback: int
    atr_period: int
    min_breakout_atr: float
    min_range_atr: float
    max_rv_ratio: float
    stop_atr: float
    take_profit_atr: float
    time_stop_candles: int
    thresholds: dict[str, float]
    weights: dict[str, float]
    fixed_risk_pct: float
    kelly: dict[str, float]
    calibration_gate: dict[str, float]
    gate: dict[str, float]
    jev_training_cutoff: Any = None

    @property
    def payoff_ratio(self) -> float:
        return self.take_profit_atr / self.stop_atr


def load_strategy(path: str | Path = "config/strategy.yaml") -> StrategyConfig:
    data = yaml.safe_load(Path(path).read_text())
    return StrategyConfig(**data)
