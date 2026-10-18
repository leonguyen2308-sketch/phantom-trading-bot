"""Watch the traders we copy and report each buy and sell within seconds of it landing.

A websocket (logsSubscribe) pushes every transaction that mentions a trader's wallet or its USDC account.
Polling catches anything the websocket missed: every 30 s while the websocket is up, every few seconds
when it is down. Old trades are never replayed: a wallet's history starts when we start watching it.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

from fomo.chain import RpcError, SolanaRpc
from fomo.swaps import USDC, Swap, parse_swap

log = logging.getLogger(__name__)


def usdc_account(wallet: str) -> str:
    return str(get_associated_token_address(Pubkey.from_string(wallet), Pubkey.from_string(USDC)))


class LeaderWatcher:
    def __init__(self, rpc: SolanaRpc, sol_usd: Callable[[], float], ws_url: str | None = None, *,
                 poll_seconds: float = 30.0, fallback_poll_seconds: float = 3.0, max_age_seconds: float = 120.0,
                 clock=time.time, use_ws: bool = True):
        self.rpc, self.sol_usd, self.ws_url, self.clock = rpc, sol_usd, ws_url, clock
        self.poll_seconds, self.fallback_poll_seconds, self.max_age = poll_seconds, fallback_poll_seconds, max_age_seconds
        self.addr_to_wallet: dict[str, str] = {}
        self.cursor: dict[str, str | None] = {}
        self.seen: deque[str] = deque(maxlen=5000)
        self._seen_set: set[str] = set()
        self.retry: dict[str, int] = {}
        self.inbox: queue.Queue[str] = queue.Queue()
        self.ws_connected = False
        self._ws_wanted: set[str] = set()
        self._ws_changed = threading.Event()
        self._last_poll = 0.0
        self.last_event_at = 0.0
        if use_ws and ws_url:
            threading.Thread(target=self._ws_loop, daemon=True, name="ws").start()

    # ---- which wallets ----
    def watch(self, wallets: set[str]) -> None:
        wanted: dict[str, str] = {}
        for w in wallets:
            wanted[w] = w
            wanted[usdc_account(w)] = w
        for addr in wanted:
            if addr not in self.cursor:  # start from now: never replay a wallet's old trades
                try:
                    latest = self.rpc.signatures(addr, limit=1)
                    self.cursor[addr] = latest[0]["signature"] if latest else None
                except RpcError:
                    if addr in self.addr_to_wallet:
                        continue
                    raise
        self.addr_to_wallet = wanted
        for a in [a for a in self.cursor if a not in wanted]:
            del self.cursor[a]
        if set(wanted) != self._ws_wanted:
            self._ws_wanted = set(wanted)
            self._ws_changed.set()

    # ---- the websocket ----
    def _ws_loop(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            log.warning("websockets is not installed: watching by polling only")
            return
        backoff = 2.0
        while True:
            addrs = sorted(self._ws_wanted)
            if not addrs:
                self._ws_changed.wait(5)
                self._ws_changed.clear()
                continue
            try:
                with connect(self.ws_url, open_timeout=15, ping_interval=20, max_size=2 ** 22) as ws:
                    for i, a in enumerate(addrs, 1):
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                            "params": [{"mentions": [a]}, {"commitment": "confirmed"}]}))
                    self._ws_changed.clear()
                    self.ws_connected, backoff = True, 2.0
                    log.info("Websocket watching %d addresses", len(addrs))
                    while not self._ws_changed.is_set():
                        try:
                            msg = json.loads(ws.recv(timeout=1))
                        except TimeoutError:
                            continue
                        if msg.get("method") == "logsNotification":
                            v = msg["params"]["result"]["value"]
                            if v.get("err") is None and v.get("signature"):
                                self.inbox.put(v["signature"])
            except Exception as e:  # dropped, refused, bad URL: fall back to fast polling and retry
                log.warning("Websocket down (%s); polling every %.0fs until it is back", type(e).__name__, self.fallback_poll_seconds)
            self.ws_connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    # ---- collect new trades ----
    def _mark_seen(self, sig: str) -> bool:
        if sig in self._seen_set:
            return False
        if len(self.seen) == self.seen.maxlen:
            self._seen_set.discard(self.seen[0])
        self.seen.append(sig)
        self._seen_set.add(sig)
        return True

    def poll(self) -> list[Swap]:
        """New trades by watched wallets, oldest first. Raises RpcError if the RPC is failing."""
        while True:  # park pushed signatures first, so a failing poll below can't lose them
            try:
                self.retry.setdefault(self.inbox.get_nowait(), 0)
            except queue.Empty:
                break
        interval = self.poll_seconds if self.ws_connected else self.fallback_poll_seconds
        if time.monotonic() - self._last_poll >= interval:
            self._last_poll = time.monotonic()
            for addr in list(self.addr_to_wallet):
                new = self.rpc.signatures(addr, limit=50, until=self.cursor.get(addr))
                if new:
                    self.cursor[addr] = new[0]["signature"]
                for s in reversed(new):
                    if s.get("err") is None:
                        self.retry.setdefault(s["signature"], 0)

        out: list[Swap] = []
        if not self.retry:
            return out
        sol = self.sol_usd()  # before anything is marked seen: if the price feed is down, nothing is lost
        for sig in list(self.retry):
            if sig in self._seen_set:
                del self.retry[sig]
                continue
            try:
                tx = self.rpc.transaction(sig)
            except RpcError:
                tx = None
            if tx is None:  # not visible to this RPC node yet: try again next poll
                self.retry[sig] = self.retry.get(sig, 0) + 1
                if self.retry[sig] > 8:
                    log.warning("Gave up on transaction %s: the RPC never returned it", sig)
                    del self.retry[sig]
                    self._mark_seen(sig)
                continue
            self.retry.pop(sig, None)
            self._mark_seen(sig)
            for wallet in dict.fromkeys(self.addr_to_wallet.values()):
                sw = parse_swap(tx, wallet, sol)
                if not sw:
                    continue
                if sw.t and self.clock() - sw.t > self.max_age:
                    log.info("Ignored a %s by %s from %ds ago: too old to copy", sw.side, wallet[:6], self.clock() - sw.t)
                    continue
                out.append(sw)
                self.last_event_at = self.clock()
        return sorted(out, key=lambda s: (s.slot, s.t))
