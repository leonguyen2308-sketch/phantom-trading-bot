"""The top traders: who we may copy, best first (rank 1 = best).

FOMO has no public API and its app hides wallet addresses, so the list comes from one of
- Solana Tracker's PnL leaderboard (SOLANATRACKER_API_KEY): the most profitable Solana wallets of the last 7 days,
  any number of them, no FOMO account needed;
- fomoapi.io (unofficial, needs FOMOAPI_KEY), whose 7-day leaderboard includes resolved Solana wallets;
- your own files: config/wallets.txt (one address per line) and config/traders.yaml (handle + wallet).
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
SOLANATRACKER = "https://data.solanatracker.io/v2/pnl/leaderboard/top"


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


def from_wallet_list(path: str | Path = "config/wallets.txt") -> list[Trader]:
    """One wallet per line, best first. Anything after the address is its name; `#` starts a comment."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[Trader] = []
    for n, line in enumerate(p.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].replace(",", " ").strip()
        if not line:
            continue
        parts = line.split()
        wallet = next((x for x in parts if is_wallet(x)), None)
        if wallet is None:
            raise ValueError(f"{p} line {n}: no Solana wallet address in '{line[:40]}'")
        name = " ".join(x for x in parts if x != wallet).lstrip("@")
        out.append(Trader(handle=name or wallet[:6], wallet=wallet, rank=len(out) + 1, source="wallets.txt"))
    return out


def _pick(row: dict, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", {}):
            return v
    return None


def parse_solanatracker(body) -> list[Trader]:
    """Accepts the shapes Solana Tracker's PnL leaderboard documents; rows without a Solana wallet are skipped."""
    rows = body
    if isinstance(body, dict):
        for k in ("traders", "data", "results", "leaderboard", "items", "wallets"):
            if isinstance(body.get(k), list):
                rows = body[k]
                break
        else:
            rows = []
    out: list[Trader] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        wallet = _pick(row, "wallet", "address", "owner", "walletAddress", "trader")
        if not isinstance(wallet, str) or not is_wallet(wallet) or wallet in seen:
            continue
        seen.add(wallet)
        period = row.get("period") if isinstance(row.get("period"), dict) else {}
        identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
        name = _pick(identity, "name", "label") or _pick(row, "name", "label", "handle") or wallet[:6]
        out.append(Trader(handle=str(name).lstrip("@"), wallet=wallet, rank=len(out) + 1,
                          pnl_7d=_num(_pick(period, "realized", "realizedPnl", "pnl"), _pick(row, "realized", "realizedPnl", "pnl", "totalPnl")),
                          win_rate=_num(_pick(period, "winRate", "win_rate"), _pick(row, "winRate", "win_rate")), source="solanatracker"))
    return out


def from_solanatracker(key: str, limit: int, days: int = 7, http: httpx.Client | None = None) -> list[Trader]:
    http = http or httpx.Client(timeout=30)
    out: list[Trader] = []
    seen: set[str] = set()
    for page in range(1, 8):  # asks for everything at once; if the API caps a page, walks the next pages
        params = {"days": days, "sort": "realized", "direction": "desc", "limit": min(limit, 500)}
        if page > 1:
            params["page"] = page
        r = http.get(SOLANATRACKER, params=params, headers={"x-api-key": key})
        r.raise_for_status()
        fresh = [t for t in parse_solanatracker(r.json()) if t.wallet not in seen]
        if not fresh:
            break
        for t in fresh:
            seen.add(t.wallet)
            out.append(Trader(**{**t.to_dict(), "rank": len(out) + 1}))
        if len(out) >= limit:
            break
    return out[:limit]


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
    r = http.get(FOMOAPI, params={"limit": min(max(limit * 3, 20), 500)}, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return parse_fomoapi(r.json())


def _renumber(traders: list[Trader]) -> list[Trader]:
    seen: set[str] = set()
    out: list[Trader] = []
    for t in traders:
        if t.wallet not in seen:
            seen.add(t.wallet)
            out.append(Trader(**{**t.to_dict(), "rank": len(out) + 1}))
    return out


def load_lineup(size: int, fomoapi_key: str = "", path: str | Path = "config/traders.yaml",
                http: httpx.Client | None = None, tracker_key: str = "", wallets_path: str | Path = "config/wallets.txt") -> list[Trader]:
    """The top `size` traders: Solana Tracker's leaderboard, else fomoapi.io, else your own files."""
    if tracker_key:
        try:
            got = from_solanatracker(tracker_key, size, http=http)
            if got:
                return got[:size]
            log.warning("Solana Tracker returned no traders with a wallet; trying the next source")
        except httpx.HTTPStatusError as e:  # 401/402/403: the key or the plan doesn't cover the leaderboard
            log.warning("Solana Tracker refused (HTTP %s); trying the next source", e.response.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Solana Tracker failed (%s); trying the next source", type(e).__name__)
    if fomoapi_key:
        try:
            got = from_fomoapi(fomoapi_key, size, http)
            if got:
                return got[:size]
            log.warning("fomoapi.io returned no traders with a Solana wallet; using your files")
        except httpx.HTTPError as e:
            log.warning("fomoapi.io failed (%s); using your files", type(e).__name__)
    mine = from_wallet_list(wallets_path)
    if Path(path).exists():
        mine = from_file(path) + mine
    return _renumber(mine)[:size]
