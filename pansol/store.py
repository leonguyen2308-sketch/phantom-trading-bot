"""Tracker storage: one SQLite file with wallets, sign-ins, portfolio snapshots and watchlists."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  wallet      TEXT PRIMARY KEY,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  sign_ins    INTEGER NOT NULL DEFAULT 0,
  value_usd   REAL,
  token_count INTEGER,
  holdings    TEXT,
  checked_at  INTEGER
);
CREATE TABLE IF NOT EXISTS watchlist (
  wallet   TEXT NOT NULL,
  mint     TEXT NOT NULL,
  added_at INTEGER NOT NULL,
  PRIMARY KEY (wallet, mint)
);
CREATE TABLE IF NOT EXISTS nonces (
  nonce      TEXT PRIMARY KEY,
  wallet     TEXT NOT NULL,
  message    TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  wallet     TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
"""

MAX_PENDING_NONCES = 20_000


class Busy(Exception):
    """Too many sign-ins waiting for a signature."""


class Store:
    def __init__(self, path: str | Path):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            if path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    # ---------- sign-in nonces (one use, five minutes) ----------

    def add_nonce(self, nonce: str, wallet: str, message: str, expires_at: int, now: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM nonces WHERE expires_at <= ?", (now,))
            pending = self.db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0]
            if pending >= MAX_PENDING_NONCES:
                raise Busy
            self.db.execute("INSERT INTO nonces VALUES (?, ?, ?, ?)", (nonce, wallet, message, expires_at))

    def take_nonce(self, nonce: str, now: int) -> dict | None:
        """Return and delete the nonce, so a signature can never be used twice."""
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM nonces WHERE nonce = ?", (nonce,)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM nonces WHERE nonce = ?", (nonce,))
        return dict(row) if row["expires_at"] > now else None

    # ---------- sessions ----------

    def add_session(self, token_hash: str, wallet: str, expires_at: int, now: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash, wallet, expires_at))

    def session_wallet(self, token_hash: str, now: int) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT wallet FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now)
            ).fetchone()
        return row["wallet"] if row else None

    def drop_session(self, token_hash: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # ---------- users ----------

    def record_sign_in(self, wallet: str, now: int) -> None:
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO users (wallet, first_seen, last_seen, sign_ins) VALUES (?, ?, ?, 1)
                   ON CONFLICT(wallet) DO UPDATE SET last_seen = excluded.last_seen, sign_ins = sign_ins + 1""",
                (wallet, now, now),
            )

    def save_snapshot(self, wallet: str, value_usd: float, holdings: list[dict], now: int) -> None:
        """Keep the latest portfolio so the admin view shows it without asking the chain again."""
        slim = [
            {k: h.get(k) for k in ("mint", "symbol", "name", "amount", "price_usd", "value_usd")}
            for h in holdings
        ]
        with self.lock, self.db:
            self.db.execute(
                "UPDATE users SET value_usd = ?, token_count = ?, holdings = ?, checked_at = ? WHERE wallet = ?",
                (value_usd, len(holdings), json.dumps(slim), now, wallet),
            )

    def touch(self, wallet: str, now: int) -> None:
        """The wallet's owner used the tracker (not called when the admin looks at it)."""
        with self.lock, self.db:
            self.db.execute("UPDATE users SET last_seen = ? WHERE wallet = ?", (now, wallet))

    def users(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                """SELECT u.*, (SELECT COUNT(*) FROM watchlist w WHERE w.wallet = u.wallet) AS watch_count
                   FROM users u ORDER BY u.last_seen DESC"""
            ).fetchall()
        return [_user(r) for r in rows]

    def user(self, wallet: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                """SELECT u.*, (SELECT COUNT(*) FROM watchlist w WHERE w.wallet = u.wallet) AS watch_count
                   FROM users u WHERE u.wallet = ?""",
                (wallet,),
            ).fetchone()
        return _user(row) if row else None

    def stats(self, now: int) -> dict:
        day_ago = now - 86400
        with self.lock:
            row = self.db.execute(
                """SELECT COUNT(*) AS users,
                          COALESCE(SUM(first_seen > ?), 0) AS new_24h,
                          COALESCE(SUM(last_seen > ?), 0) AS active_24h,
                          COALESCE(SUM(value_usd), 0) AS tracked_usd
                   FROM users""",
                (day_ago, day_ago),
            ).fetchone()
        return dict(row)

    # ---------- watchlist ----------

    def watchlist(self, wallet: str) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT mint FROM watchlist WHERE wallet = ? ORDER BY added_at, mint", (wallet,)
            ).fetchall()
        return [r["mint"] for r in rows]

    def add_watch(self, wallet: str, mint: str, now: int, limit: int) -> bool:
        """False when the watchlist is full. Adding a coin twice is a no-op."""
        with self.lock, self.db:
            exists = self.db.execute("SELECT 1 FROM watchlist WHERE wallet = ? AND mint = ?", (wallet, mint)).fetchone()
            if exists:
                return True
            count = self.db.execute("SELECT COUNT(*) FROM watchlist WHERE wallet = ?", (wallet,)).fetchone()[0]
            if count >= limit:
                return False
            self.db.execute("INSERT INTO watchlist VALUES (?, ?, ?)", (wallet, mint, now))
            return True

    def remove_watch(self, wallet: str, mint: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM watchlist WHERE wallet = ? AND mint = ?", (wallet, mint))


def _user(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["holdings"] = json.loads(d["holdings"]) if d.get("holdings") else []
    return d
