"""The weekly top: who we may copy, best first.

FOMO has no public API and its app hides wallet addresses, so the list comes from either
- config/traders.yaml, which you fill in (handles and wallets from a FOMO wallet finder), or
- fomoapi.io (unofficial, needs FOMOAPI_KEY), whose 7-day leaderboard includes resolved Solana wallets.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import yaml
from solders.pubkey import Pubkey

log = logging.getLogger(__name__)

FOMOAPI = "https://api.fomoapi.io/v2/leaderboard/7d"


@dataclass
class Trader:
    handle: str
    wallet: str
    rank: int
    pnl_7d: float | None = None
    win_rate: float | None = None
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_wallet(s: str) -> bool:
    try:
        return len(s) >= 32 and Pubkey.from_string(s) is not None
    except Exception:
        return False


def from_file(path: str | Path = "config/traders.yaml") -> list[Trader]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: list[Trader] = []
    for i, row in enumerate(data.get("traders") or [], 1):
        wallet = str(row.get("wallet", "")).strip()
        if not is_wallet(wallet):
            raise ValueError(f"config/traders.yaml: '{wallet}' for @{row.get('handle')} is not a Solana wallet address")
        out.append(Trader(handle=str(row.get("handle") or wallet[:6]).lstrip("@"), wallet=wallet, rank=i,
                          pnl_7d=row.get("pnl_7d"), win_rate=row.get("win_rate"), source="file"))
    return out


def _num(*vals):
    for v in vals:
        if isinstance(v, dict):
            v = v.get("7d")
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def parse_fomoapi(body) -> list[Trader]:
    """Accepts the response shapes fomoapi.io has documented; rows without a Solana wallet are skipped."""
    rows = body
    if isinstance(body, dict):
        for k in ("leaderboard", "data", "results", "traders", "items"):
            if isinstance(body.get(k), list):
                rows = body[k]
                break
        else:
            inner = body.get("responseObject") or {}
            rows = inner.get("leaderboard") if isinstance(inner, dict) else []
    out: list[Trader] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        wallets = row.get("wallets") or {}
        wallet = (wallets.get("solana") if isinstance(wallets, dict) else None) or row.get("solanaWallet") \
            or row.get("solana_wallet") or row.get("wallet") or row.get("walletAddress") or ""
        if isinstance(wallet, list):
            wallet = wallet[0] if wallet else ""
        if not is_wallet(str(wallet)):
            continue
        handle = str(row.get("handle") or row.get("userHandle") or row.get("username") or row.get("displayName") or wallet[:6]).lstrip("@")
        out.append(Trader(handle=handle, wallet=str(wallet), rank=len(out) + 1,
                          pnl_7d=_num(row.get("pnlUsd"), row.get("pnl_usd"), row.get("pnl7d"), row.get("pnl")),
                          win_rate=_num(row.get("winRate"), row.get("win_rate")), source="fomoapi.io"))
    return out


def from_fomoapi(key: str, limit: int, http: httpx.Client | None = None) -> list[Trader]:
    http = http or httpx.Client(timeout=20)
    r = http.get(FOMOAPI, params={"limit": max(limit * 3, 20)}, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return parse_fomoapi(r.json())


def load_lineup(size: int, fomoapi_key: str = "", path: str | Path = "config/traders.yaml",
                http: httpx.Client | None = None) -> list[Trader]:
    if fomoapi_key:
        try:
            got = from_fomoapi(fomoapi_key, size, http)
            if got:
                return got[:size]
            log.warning("fomoapi.io returned no traders with a Solana wallet; using config/traders.yaml")
        except httpx.HTTPError as e:
            log.warning("fomoapi.io failed (%s); using config/traders.yaml", type(e).__name__)
    return from_file(path)[:size]
