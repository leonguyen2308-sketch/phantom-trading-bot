"""Everything the desk did, in SQLite (state/desk.sqlite).

positions: real buys (book='real') and SHADOW positions (book='shadow'): every token that passed all the
code gates gets a shadow position with the same exit plan, whether or not the Claude desk approved it. Real vs
shadow is the running test of whether the desk adds anything over rules alone.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, mode TEXT, book TEXT, mint TEXT, symbol TEXT, decimals INTEGER, station TEXT, pool TEXT,
  opened_t INTEGER, cost_usd REAL, raw INTEGER, entry_price REAL, plan TEXT,
  sold_raw INTEGER DEFAULT 0, sold_usd REAL DEFAULT 0, exit_pending REAL DEFAULT 0,
  closed_t INTEGER, pnl REAL, exit_kind TEXT, decision_id INTEGER, buy_ref TEXT, last_ref TEXT, source_wallet TEXT
);
CREATE TABLE IF NOT EXISTS exits (id INTEGER PRIMARY KEY, mode TEXT, book TEXT, position_id INTEGER, t INTEGER,
  kind TEXT, raw INTEGER, usd REAL, cost_share REAL, ref TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, mode TEXT, t INTEGER, mint TEXT, symbol TEXT, station TEXT, snapshot TEXT, checks TEXT,
  opinion TEXT, verdict TEXT, conviction REAL, action TEXT, note TEXT, size_usd REAL
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, mode TEXT, t INTEGER, kind TEXT, seat TEXT, text TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS equity (t INTEGER, mode TEXT, v REAL);
CREATE TABLE IF NOT EXISTS funnel (day TEXT, mode TEXT, gate TEXT, n INTEGER, PRIMARY KEY (day, mode, gate));
CREATE TABLE IF NOT EXISTS kills (day TEXT, mode TEXT, gate TEXT, reason TEXT, n INTEGER, PRIMARY KEY (day, mode, gate, reason));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def _locked(fn):
    @wraps(fn)
    def inner(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return inner


def day_of(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


class Journal:
    """One SQLite connection shared by the scanner thread, the manager and the dashboard: every method holds a lock."""

    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- key/value ----
    @_locked
    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    @_locked
    def get_prefix(self, prefix: str) -> dict:
        return {r["k"]: json.loads(r["v"]) for r in self.db.execute("SELECT k, v FROM kv WHERE k LIKE ? ESCAPE '\\'",
                                                                   (prefix.replace("_", "\\_").replace("%", "\\%") + "%",))}

    @_locked
    def put(self, k: str, v) -> None:
        self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        self.db.commit()

    # ---- feed ----
    @_locked
    def event(self, mode: str, t: float, kind: str, seat: str, text: str, pnl: float | None = None) -> None:
        self.db.execute("INSERT INTO events(mode, t, kind, seat, text, pnl) VALUES(?,?,?,?,?,?)", (mode, int(t), kind, seat, text, pnl))
        self.db.commit()

    @_locked
    def events(self, mode: str, n: int = 80, seats: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if seats:
            q = f"SELECT * FROM events WHERE mode=? AND seat IN ({','.join('?' * len(seats))}) ORDER BY id DESC LIMIT ?"
            return self.db.execute(q, (mode, *seats, n)).fetchall()
        return self.db.execute("SELECT * FROM events WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, n)).fetchall()

    # ---- funnel counters ----
    @_locked
    def count(self, mode: str, t: float, gate: str, n: int = 1) -> None:
        self.db.execute("INSERT INTO funnel VALUES(?,?,?,?) ON CONFLICT(day, mode, gate) DO UPDATE SET n = n + excluded.n",
                        (day_of(t), mode, gate, n))
        self.db.commit()

    @_locked
    def kill(self, mode: str, t: float, gate: str, reasons: list[str]) -> None:
        for r in reasons[:3]:
            key = re.sub(r"\s+", " ", re.sub(r"[-+]?\$?\d[\d,.]*[%x]?", "N", r.split("(")[0])).strip() or r
            self.db.execute("INSERT INTO kills VALUES(?,?,?,?,1) ON CONFLICT(day, mode, gate, reason) DO UPDATE SET n = n + 1",
                            (day_of(t), mode, gate, key[:60]))
        self.db.commit()

    @_locked
    def funnel(self, mode: str, day: str) -> dict[str, int]:
        return {r["gate"]: r["n"] for r in self.db.execute("SELECT gate, n FROM funnel WHERE day=? AND mode=?", (day, mode))}

    @_locked
    def top_kills(self, mode: str, day: str, n: int = 8) -> list[sqlite3.Row]:
        return self.db.execute("SELECT gate, reason, n FROM kills WHERE day=? AND mode=? ORDER BY n DESC LIMIT ?", (day, mode, n)).fetchall()

    # ---- decisions ----
    @_locked
    def decision(self, **row) -> int:
        for k in ("snapshot", "checks", "opinion"):
            if k in row and not isinstance(row[k], str):
                row[k] = json.dumps(row[k])
        cur = self.db.execute(f"INSERT INTO decisions({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    @_locked
    def decisions(self, mode: str, n: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, n)).fetchall()

    @_locked
    def last_decision_t(self, mode: str, mint: str) -> int | None:
        row = self.db.execute("SELECT MAX(t) AS t FROM decisions WHERE mode=? AND mint=?", (mode, mint)).fetchone()
        return row["t"] if row else None

    # ---- positions ----
    @_locked
    def open_position(self, **row) -> int:
        if not isinstance(row.get("plan"), str):
            row["plan"] = json.dumps(row["plan"])
        cur = self.db.execute(f"INSERT INTO positions({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    @_locked
    def update_position(self, pid: int, **row) -> None:
        if "plan" in row and not isinstance(row["plan"], str):
            row["plan"] = json.dumps(row["plan"])
        self.db.execute(f"UPDATE positions SET {','.join(f'{k}=?' for k in row)} WHERE id=?", (*row.values(), pid))
        self.db.commit()

    @_locked
    def position(self, pid: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()

    @_locked
    def open_positions(self, mode: str, book: str = "real") -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM positions WHERE mode=? AND book=? AND closed_t IS NULL ORDER BY id", (mode, book)).fetchall()

    @_locked
    def closed_positions(self, mode: str, book: str = "real") -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM positions WHERE mode=? AND book=? AND closed_t IS NOT NULL ORDER BY closed_t", (mode, book)).fetchall()

    @_locked
    def last_close_t(self, mode: str, mint: str) -> int | None:
        row = self.db.execute("SELECT MAX(closed_t) AS t FROM positions WHERE mode=? AND book='real' AND mint=?", (mode, mint)).fetchone()
        return row["t"] if row else None

    @_locked
    def record_exit(self, **row) -> None:
        self.db.execute(f"INSERT INTO exits({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()

    @_locked
    def exits_by_kind(self, mode: str, book: str = "real") -> list[sqlite3.Row]:
        return self.db.execute("SELECT kind, COUNT(*) AS n, SUM(usd - cost_share) AS pnl FROM exits WHERE mode=? AND book=? GROUP BY kind",
                               (mode, book)).fetchall()

    # ---- equity ----
    @_locked
    def equity(self, mode: str, t: float, v: float) -> None:
        self.db.execute("INSERT INTO equity VALUES(?,?,?)", (int(t), mode, v))
        self.db.commit()

    @_locked
    def equity_curve(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT t, v FROM equity WHERE mode=? ORDER BY t", (mode,)).fetchall()

    @_locked
    def conviction_outcomes(self, mode: str) -> list[sqlite3.Row]:
        """Each desk conviction next to how its shadow (rules-only) position ended: is the confidence worth anything?"""
        return self.db.execute("SELECT d.conviction AS c, p.pnl AS pnl, d.action AS action FROM decisions d JOIN positions p "
                               "ON p.decision_id = d.id WHERE d.mode=? AND p.book='shadow' AND p.closed_t IS NOT NULL "
                               "AND d.conviction IS NOT NULL", (mode,)).fetchall()


def trade_stats(rows: list) -> dict:
    """Honest numbers instead of a win rate: expectancy per trade (as % of what was spent) with a 95% interval."""
    rets = [r["pnl"] / r["cost_usd"] * 100 for r in rows if r["cost_usd"]]
    n = len(rets)
    if not n:
        return {"n": 0}
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    holds = [(r["closed_t"] - r["opened_t"]) / 60 for r in rows if r["closed_t"]]
    return {"n": n, "wins": sum(1 for x in rets if x > 0), "expectancy_pct": round(mean, 2),
            "ci95_pct": [round(mean - half, 2), round(mean + half, 2)] if half is not None else None,
            "median_pct": round(statistics.median(rets), 2), "worst_pct": round(min(rets), 2), "best_pct": round(max(rets), 2),
            "pnl_usd": round(sum(r["pnl"] for r in rows), 2), "avg_hold_min": round(statistics.fmean(holds), 1) if holds else None}
