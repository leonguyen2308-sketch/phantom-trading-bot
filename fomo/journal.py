"""What the copy bot did, in SQLite (state/fomo.sqlite): every copy, every feed line, the equity curve."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
  id INTEGER PRIMARY KEY, mode TEXT, mint TEXT, symbol TEXT, decimals INTEGER, trader TEXT, handle TEXT,
  opened_t INTEGER, cost_usd REAL, raw INTEGER, trader_price REAL, trader_sig TEXT, buy_ref TEXT,
  sold_usd REAL DEFAULT 0, sold_raw INTEGER DEFAULT 0, exit_pending REAL DEFAULT 0,
  closed_t INTEGER, pnl REAL, reason TEXT, last_ref TEXT
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, mode TEXT, t INTEGER, kind TEXT, text TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS equity (t INTEGER, mode TEXT, v REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, k: str, v) -> None:
        self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        self.db.commit()

    def event(self, mode: str, t: float, kind: str, text: str, pnl: float | None = None) -> None:
        self.db.execute("INSERT INTO events(mode, t, kind, text, pnl) VALUES(?,?,?,?,?)", (mode, int(t), kind, text, pnl))
        self.db.commit()

    def events(self, mode: str, n: int = 60) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM events WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, n)).fetchall()

    def open_copy(self, **row) -> int:
        cur = self.db.execute(f"INSERT INTO copies({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    def update_copy(self, copy_id: int, **row) -> None:
        self.db.execute(f"UPDATE copies SET {','.join(f'{k}=?' for k in row)} WHERE id=?", (*row.values(), copy_id))
        self.db.commit()

    def copy(self, copy_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()

    def open_copies(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM copies WHERE mode=? AND closed_t IS NULL ORDER BY id", (mode,)).fetchall()

    def closed_copies(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM copies WHERE mode=? AND closed_t IS NOT NULL ORDER BY closed_t", (mode,)).fetchall()

    def count_copies(self, mode: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM copies WHERE mode=?", (mode,)).fetchone()[0]

    def last_exit(self, mode: str, mint: str) -> int | None:
        row = self.db.execute("SELECT MAX(closed_t) AS t FROM copies WHERE mode=? AND mint=?", (mode, mint)).fetchone()
        return row["t"] if row else None

    def equity(self, mode: str, t: float, v: float) -> None:
        self.db.execute("INSERT INTO equity VALUES(?,?,?)", (int(t), mode, v))
        self.db.commit()

    def equity_curve(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT t, v FROM equity WHERE mode=? ORDER BY t", (mode,)).fetchall()
