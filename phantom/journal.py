"""Decision log (SQLite). Every candle's snapshot, Jev answers, action and outcome is recorded."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, t INTEGER, mode TEXT, close REAL, schema TEXT, snap_hash TEXT, snapshot TEXT,
  jev TEXT, score REAL, latency_ms REAL, candidate INTEGER, action TEXT, note TEXT, result REAL,
  tp_prob REAL, tp_label INTEGER, label_due INTEGER, stop_level REAL, tp_level REAL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, mode TEXT, entry_t INTEGER, entry REAL, qty REAL, stop REAL, tp REAL, prob REAL,
  risk_usd REAL, kelly_risk REAL, entry_ref TEXT, exit_t INTEGER, exit REAL, reason TEXT, pnl REAL, r REAL, exit_ref TEXT,
  strategy_version TEXT
);
CREATE TABLE IF NOT EXISTS equity (t INTEGER, mode TEXT, v REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        if "strategy_version" not in cols:
            self.db.execute("ALTER TABLE trades ADD COLUMN strategy_version TEXT")

    # ---- key/value state (position, peak equity, ...) ----
    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, k: str, v) -> None:
        self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        self.db.commit()

    # ---- writes ----
    def decision(self, **row) -> int:
        for k in ("snapshot", "jev"):
            if k in row and not isinstance(row[k], str):
                row[k] = json.dumps(row[k])
        cols = ",".join(row)
        cur = self.db.execute(f"INSERT INTO decisions({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    def set_result(self, decision_id: int, result: float) -> None:
        self.db.execute("UPDATE decisions SET result=? WHERE id=?", (result, decision_id))
        self.db.commit()

    def open_trade(self, **row) -> int:
        cur = self.db.execute(f"INSERT INTO trades({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, **row) -> None:
        sets = ",".join(f"{k}=?" for k in row)
        self.db.execute(f"UPDATE trades SET {sets} WHERE id=?", (*row.values(), trade_id))
        self.db.commit()

    def equity(self, t: int, mode: str, v: float) -> None:
        self.db.execute("INSERT INTO equity VALUES(?,?,?)", (t, mode, v))
        self.db.commit()

    # ---- labels for calibration ----
    def pending_labels(self, now: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions WHERE tp_prob IS NOT NULL AND tp_label IS NULL AND label_due <= ?", (now,)).fetchall()

    def set_label(self, decision_id: int, label: int) -> None:
        self.db.execute("UPDATE decisions SET tp_label=? WHERE id=?", (label, decision_id))
        self.db.commit()

    # ---- reads ----
    def calibration_samples(self, mode: str | None = None, candidates_only: bool = True) -> tuple[list[float], list[int]]:
        q = "SELECT tp_prob, tp_label FROM decisions WHERE tp_label >= 0"
        args: list = []
        if candidates_only:
            q += " AND candidate=1"
        if mode:
            q += " AND mode=?"
            args.append(mode)
        rows = self.db.execute(q, args).fetchall()
        return [r[0] for r in rows], [r[1] for r in rows]

    def closed_trades(self, mode: str, version: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM trades WHERE mode=? AND exit_t IS NOT NULL", [mode]
        if version:
            q += " AND strategy_version=?"
            args.append(version)
        return self.db.execute(q + " ORDER BY exit_t", args).fetchall()

    def recent_decisions(self, mode: str, n: int = 60) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, n)).fetchall()

    def equity_curve(self, mode: str, since: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT t, v FROM equity WHERE mode=? AND t>=? ORDER BY t", (mode, since)).fetchall()

    def first_decision_time(self, mode: str) -> int | None:
        row = self.db.execute("SELECT MIN(t) AS t FROM decisions WHERE mode=?", (mode,)).fetchone()
        return row["t"] if row and row["t"] else None


def now() -> int:
    return int(time.time())
