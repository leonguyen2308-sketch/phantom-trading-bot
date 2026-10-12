"""Tracker storage: wallets, sign-ins, portfolio snapshots and watchlists.

Runs on a SQLite file (local use, or a host with a disk) or on PostgreSQL when given a
postgres:// URL (free hosts whose disk is wiped on every restart, with a free Neon database).
The SQL below is written once and runs on both.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
         wallet      TEXT PRIMARY KEY,
         first_seen  BIGINT NOT NULL,
         last_seen   BIGINT NOT NULL,
         sign_ins    BIGINT NOT NULL DEFAULT 0,
         value_usd   DOUBLE PRECISION,
         token_count BIGINT,
         holdings    TEXT,
         checked_at  BIGINT
       )""",
    """CREATE TABLE IF NOT EXISTS watchlist (
         wallet   TEXT NOT NULL,
         mint     TEXT NOT NULL,
         added_at BIGINT NOT NULL,
         PRIMARY KEY (wallet, mint)
       )""",
    """CREATE TABLE IF NOT EXISTS used_nonces (
         nonce      TEXT PRIMARY KEY,
         expires_at BIGINT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS sessions (
         token_hash TEXT PRIMARY KEY,
         wallet     TEXT NOT NULL,
         expires_at BIGINT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS meta (
         key   TEXT PRIMARY KEY,
         value TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS users_last_seen ON users (last_seen)",
]

SUMMARY = "u.wallet, u.first_seen, u.last_seen, u.sign_ins, u.value_usd, u.token_count, u.checked_at"
WATCH_COUNT = "(SELECT COUNT(*) FROM watchlist w WHERE w.wallet = u.wallet) AS watch_count"


class Unavailable(Exception):
    """The database did not answer in time (it is down, asleep too long, or unreachable)."""


def is_postgres(target: str) -> bool:
    return target.lower().startswith(("postgres://", "postgresql://"))


class _Tx:
    """One transaction. SQL uses ? placeholders; they become %s on PostgreSQL."""

    def __init__(self, conn, postgres: bool):
        self.conn = conn
        self.postgres = postgres

    def _exec(self, sql: str, params: tuple):
        return self.conn.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._exec(sql, params).fetchone()
        return dict(row) if row else None

    def run(self, sql: str, params: tuple = ()) -> int:
        """Rows changed."""
        return self._exec(sql, params).rowcount


class _SQLite:
    postgres = False

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def tx(self) -> Iterator[_Tx]:
        with self.lock, self.conn:          # commits, or rolls back on an error
            yield _Tx(self.conn, False)


class _Postgres:
    postgres = True

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool, PoolTimeout
        except ImportError as e:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "PostgreSQL support isn't installed. Run with: uv run --extra postgres python -m pansol "
                '(or pip install "psycopg[binary,pool]")'
            ) from e
        try:
            conninfo_to_dict(url)
        except psycopg.ProgrammingError:
            raise ValueError("DATABASE_URL is not a valid PostgreSQL address. "
                             "Copy it again; special characters in the password must be percent-encoded.") from None
        # One plain connection first, so a wrong password, host or database name fails now with the real reason.
        psycopg.connect(url, connect_timeout=15).close()

        self.down = (PoolTimeout, psycopg.OperationalError)
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            timeout=5,                        # a request waits at most 5 s for a connection, then gets a 503
            # Free databases drop idle connections: test each one before use, and close spare ones
            # within a minute so few are left when Neon suspends (after 5 idle minutes).
            check=ConnectionPool.check_connection,
            max_idle=60,
            kwargs={
                "row_factory": dict_row,
                # Poolers (e.g. Neon's "-pooler" address) can't keep prepared statements between transactions.
                "prepare_threshold": None,
                "connect_timeout": 5,
                "tcp_user_timeout": 5000,     # give up on a silent network after 5 s instead of minutes
            },
            open=False,
        )
        self.pool.open(wait=True, timeout=15)

    @contextmanager
    def tx(self) -> Iterator[_Tx]:
        try:
            with self.pool.connection() as conn:  # commits, or rolls back on an error
                yield _Tx(conn, True)
        except self.down as e:
            raise Unavailable(str(e)) from e


class Store:
    def __init__(self, target: str | Path):
        target = str(target)
        self.db = _Postgres(target) if is_postgres(target) else _SQLite(target)
        try:
            with self.db.tx() as t:
                for statement in SCHEMA:
                    t.run(statement)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.db.postgres:
            self.db.pool.close()
        else:
            self.db.conn.close()

    def secret(self, make: Callable[[], bytes]) -> bytes:
        """The key that signs sign-in challenges, created once and kept with the data."""
        with self.db.tx() as t:
            t.run("INSERT INTO meta (key, value) VALUES ('challenge_secret', ?) ON CONFLICT (key) DO NOTHING",
                  (make().hex(),))
            value = t.one("SELECT value FROM meta WHERE key = 'challenge_secret'")["value"]
        return bytes(value) if isinstance(value, (bytes, memoryview)) else bytes.fromhex(value)

    # ---------- sign-in nonces: stored only once a signature checks out ----------

    def use_nonce(self, nonce: str, expires_at: int, now: int) -> bool:
        """Mark the nonce used. False if it already was, so a signature can never be replayed."""
        with self.db.tx() as t:
            t.run("DELETE FROM used_nonces WHERE expires_at <= ?", (now,))
            added = t.run("INSERT INTO used_nonces (nonce, expires_at) VALUES (?, ?) ON CONFLICT (nonce) DO NOTHING",
                          (nonce, expires_at))
        return added == 1

    # ---------- sessions ----------

    def add_session(self, token_hash: str, wallet: str, expires_at: int, now: int) -> None:
        with self.db.tx() as t:
            t.run("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            t.run("INSERT INTO sessions (token_hash, wallet, expires_at) VALUES (?, ?, ?)",
                  (token_hash, wallet, expires_at))

    def session_wallet(self, token_hash: str, now: int) -> str | None:
        with self.db.tx() as t:
            row = t.one("SELECT wallet FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now))
        return row["wallet"] if row else None

    def drop_session(self, token_hash: str) -> None:
        with self.db.tx() as t:
            t.run("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # ---------- users ----------

    def record_sign_in(self, wallet: str, now: int) -> None:
        with self.db.tx() as t:
            t.run(
                """INSERT INTO users (wallet, first_seen, last_seen, sign_ins) VALUES (?, ?, ?, 1)
                   ON CONFLICT (wallet) DO UPDATE SET last_seen = excluded.last_seen, sign_ins = users.sign_ins + 1""",
                (wallet, now, now),
            )

    def save_snapshot(self, wallet: str, value_usd: float, token_count: int, holdings: list[dict], now: int) -> None:
        """Keep the latest portfolio so the admin view shows it without asking the chain again."""
        slim = [
            {k: h.get(k) for k in ("mint", "symbol", "name", "amount", "price_usd", "value_usd")}
            for h in holdings
        ]
        with self.db.tx() as t:
            t.run(
                "UPDATE users SET value_usd = ?, token_count = ?, holdings = ?, checked_at = ? WHERE wallet = ?",
                (value_usd, token_count, json.dumps(slim), now, wallet),
            )

    def touch(self, wallet: str, now: int) -> None:
        """The wallet's owner used the tracker (not called when the admin looks at it)."""
        with self.db.tx() as t:
            t.run("UPDATE users SET last_seen = ? WHERE wallet = ?", (now, wallet))

    def users(self, limit: int | None = None) -> list[dict]:
        """Most recently seen first, without the holdings snapshot (that is per user, in user())."""
        sql = f"SELECT {SUMMARY}, {WATCH_COUNT} FROM users u ORDER BY u.last_seen DESC, u.wallet"
        with self.db.tx() as t:
            return t.all(sql + " LIMIT ?", (limit,)) if limit else t.all(sql)

    def users_with_holdings(self) -> Iterator[dict]:
        """Every user with their snapshot, a page at a time (for the CSV export)."""
        offset = 0
        while True:
            with self.db.tx() as t:
                rows = t.all(
                    f"SELECT u.*, {WATCH_COUNT} FROM users u ORDER BY u.last_seen DESC, u.wallet LIMIT 500 OFFSET ?",
                    (offset,),
                )
            if not rows:
                return
            yield from (_user(r) for r in rows)
            offset += len(rows)

    def user(self, wallet: str) -> dict | None:
        with self.db.tx() as t:
            row = t.one(f"SELECT u.*, {WATCH_COUNT} FROM users u WHERE u.wallet = ?", (wallet,))
        return _user(row) if row else None

    def stats(self, now: int) -> dict:
        day_ago = now - 86400
        with self.db.tx() as t:
            row = t.one(
                """SELECT COUNT(*) AS users,
                          COALESCE(SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END), 0) AS new_24h,
                          COALESCE(SUM(CASE WHEN last_seen > ? THEN 1 ELSE 0 END), 0) AS active_24h,
                          COALESCE(SUM(value_usd), 0) AS tracked_usd
                   FROM users""",
                (day_ago, day_ago),
            )
        return {"users": int(row["users"]), "new_24h": int(row["new_24h"]),
                "active_24h": int(row["active_24h"]), "tracked_usd": float(row["tracked_usd"])}

    # ---------- watchlist ----------

    def watchlist(self, wallet: str) -> list[str]:
        with self.db.tx() as t:
            rows = t.all("SELECT mint FROM watchlist WHERE wallet = ? ORDER BY added_at, mint", (wallet,))
        return [r["mint"] for r in rows]

    def add_watch(self, wallet: str, mint: str, now: int, limit: int) -> bool:
        """False when the watchlist is full. Adding a coin twice is a no-op."""
        with self.db.tx() as t:
            # Lock this user's row first, so two adds at once can't both pass the limit check.
            t.run("UPDATE users SET last_seen = last_seen WHERE wallet = ?", (wallet,))
            if t.one("SELECT 1 AS hit FROM watchlist WHERE wallet = ? AND mint = ?", (wallet, mint)):
                return True
            if t.one("SELECT COUNT(*) AS n FROM watchlist WHERE wallet = ?", (wallet,))["n"] >= limit:
                return False
            t.run("INSERT INTO watchlist (wallet, mint, added_at) VALUES (?, ?, ?) ON CONFLICT (wallet, mint) DO NOTHING",
                  (wallet, mint, now))
            return True

    def remove_watch(self, wallet: str, mint: str) -> None:
        with self.db.tx() as t:
            t.run("DELETE FROM watchlist WHERE wallet = ? AND mint = ?", (wallet, mint))


def _user(row: dict) -> dict:
    row["holdings"] = json.loads(row["holdings"]) if row.get("holdings") else []
    return row
