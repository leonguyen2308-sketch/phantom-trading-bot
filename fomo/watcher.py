"""Watch the traders we copy and report each buy and sell within seconds of it landing.

Websockets (logsSubscribe) push every transaction that mentions a trader's wallet (and, for a few wallets, its
USDC account). A node accepts one address per subscription, so 500 wallets means 500 subscriptions: they are
spread over several connections (the free Helius plan allows 5). Polling catches anything a websocket missed:
every 30 s while the websockets are up, every few seconds when they are down. With a few wallets that means
every wallet; with hundreds it means only the wallets whose coins we hold (their sells are what we must not
miss), because reading 500 wallets by polling would use the whole RPC allowance (and a buy seen a minute late
is not worth copying anyway). Old trades are never replayed: a wallet's history starts when we start watching it.

Reading a transaction costs one RPC credit, so a crowd is read under a budget: at most a few per second and at
most `max_reads_per_hour`, with the trades of coins we hold always first. A wallet that trades more than
`max_wallet_txs_per_hour` is a bot (or too fast to copy from a Mac) and is ignored for six hours.
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

EAGER_MAX = 50  # up to this many addresses: start every cursor at once and poll them all (the old, small-list behaviour)
IGNORE_SECONDS = 6 * 3600
SUBSCRIBE_GAP = 0.01  # seconds between subscribe messages on one connection


def usdc_account(wallet: str) -> str:
    return str(get_associated_token_address(Pubkey.from_string(wallet), Pubkey.from_string(USDC)))


class _Shard:
    """One websocket connection and the addresses it carries."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.addrs: set[str] = set()
        self.pending: queue.Queue[str] = queue.Queue()  # new addresses to subscribe on the live connection
        self.connected = False


class LeaderWatcher:
    def __init__(self, rpc: SolanaRpc, sol_usd: Callable[[], float], ws_url: str | None = None, *,
                 poll_seconds: float = 30.0, fallback_poll_seconds: float = 3.0, max_age_seconds: float = 120.0,
                 clock=time.time, use_ws: bool = True, ws_connections: int = 1, wallets_per_connection: int = 10 ** 9,
                 usdc_accounts: bool = True, max_wallet_txs_per_hour: int = 0, max_reads_per_hour: int = 0,
                 max_reads_per_poll: int = 0):
        self.rpc, self.sol_usd, self.ws_url, self.clock = rpc, sol_usd, ws_url, clock
        self.poll_seconds, self.fallback_poll_seconds, self.max_age = poll_seconds, fallback_poll_seconds, max_age_seconds
        self.usdc_accounts = usdc_accounts
        self.max_wallet_txs, self.max_reads_hour, self.max_reads_poll = max_wallet_txs_per_hour, max_reads_per_hour, max_reads_per_poll
        self.priority: set[str] = set()  # wallets whose trades come first: we hold a coin they bought
        self.addr_to_wallet: dict[str, str] = {}
        self.cursor: dict[str, str | None] = {}
        self.seen: deque[str] = deque(maxlen=20000)
        self._seen_set: set[str] = set()
        self.retry: dict[str, int] = {}
        self.inbox: queue.Queue[str] = queue.Queue()
        self._hint: dict[str, set[str]] = {}  # signature -> the watched wallets whose subscription reported it
        self._first_seen: dict[str, float] = {}
        self._reads: deque[float] = deque()
        self.skipped_reads = 0
        self._guard: dict[str, deque[float]] = {}
        self._ignored: dict[str, float] = {}
        self._guard_lock = threading.Lock()
        self._shards = [_Shard(wallets_per_connection) for _ in range(max(1, ws_connections))]
        self._assign: dict[str, int] = {}
        self._overflow: set[str] = set()  # addresses that fit on no connection: polled, not streamed
        self._rejected: set[str] = set()  # addresses the node refused to subscribe
        self._last_key = None
        self._last_poll = 0.0
        self.last_event_at = 0.0
        self.use_ws = bool(use_ws and ws_url)
        if self.use_ws:
            for i, shard in enumerate(self._shards):
                threading.Thread(target=self._ws_loop, args=(shard, i), daemon=True, name=f"ws{i}").start()

    # ---- state for the dashboard ----
    @property
    def ws_connected(self) -> bool:
        live = [s for s in self._shards if s.addrs]
        return bool(live) and all(s.connected for s in live)

    @property
    def ignored_count(self) -> int:
        now = self.clock()
        with self._guard_lock:  # the websocket threads add to it
            return sum(1 for until in self._ignored.values() if until > now)

    def status(self) -> str:
        live = [s for s in self._shards if s.addrs]
        wallets = len(set(self.addr_to_wallet.values()))
        if not self.use_ws:
            return f"polling {wallets} wallets (no websocket)"
        up = sum(1 for s in live if s.connected)
        text = f"{up}/{len(live)} websockets up · {wallets} wallets"
        if self._overflow:
            text += f" · {len(self._overflow)} addresses over capacity"
        if self._rejected:
            text += f" · {len(self._rejected)} refused by the node"
        if self.ignored_count:
            text += f" · {self.ignored_count} bots ignored"
        if self.max_reads_hour:
            used = sum(1 for t in self._reads if t > self.clock() - 3600)
            text += f" · reads {used}/{self.max_reads_hour} this hour"
        return text

    # ---- which wallets ----
    def _addresses(self, wallets: set[str]) -> dict[str, str]:
        """Address -> wallet, the wallets we hold coins from first (they get a connection slot before anyone else)."""
        order = sorted(wallets, key=lambda w: (w not in self.priority, w))
        out: dict[str, str] = {}
        for w in order:
            out[w] = w
            if self.usdc_accounts:
                out[usdc_account(w)] = w
        return out

    def watch(self, wallets: set[str]) -> None:
        key = (frozenset(wallets), frozenset(self.priority))
        if key == self._last_key:
            return
        wanted = self._addresses(set(wallets))
        eager = len(wanted) <= EAGER_MAX
        for addr, wallet in wanted.items():
            if addr not in self.cursor and (eager or wallet in self.priority):  # start from now: never replay old trades
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
        self._assign_shards(wanted)
        self._last_key = key

    def _assign_shards(self, wanted: dict[str, str]) -> None:
        for addr in [a for a in self._assign if a not in wanted]:
            self._shards[self._assign.pop(addr)].addrs.discard(addr)
        self._overflow &= set(wanted)
        self._rejected &= set(wanted)
        for addr in wanted:  # wanted is ordered: priority wallets first
            if addr in self._assign:
                continue
            room = [(len(s.addrs), i) for i, s in enumerate(self._shards) if len(s.addrs) < s.capacity]
            if not room:
                if addr not in self._overflow:
                    log.warning("No websocket room for %s: polling it instead", addr[:6])
                self._overflow.add(addr)
                continue
            _, i = min(room)
            self._overflow.discard(addr)
            self._assign[addr] = i
            self._shards[i].addrs.add(addr)
            self._shards[i].pending.put(addr)

    # ---- the websockets ----
    def _ws_loop(self, shard: _Shard, index: int) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            log.warning("websockets is not installed: watching by polling only")
            return
        time.sleep(0.4 * index)  # connections open one after the other, not all at once
        backoff = 2.0
        while True:
            if not shard.addrs:
                time.sleep(1.0)
                continue
            try:
                with connect(self.ws_url, open_timeout=15, ping_interval=20, max_size=2 ** 22) as ws:
                    req_addr: dict[int, str] = {}
                    sub_addr: dict[int, str] = {}
                    subscribed: set[str] = set()
                    next_id = 0

                    def subscribe(addr: str) -> None:
                        nonlocal next_id
                        next_id += 1
                        req_addr[next_id] = addr
                        subscribed.add(addr)
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": "logsSubscribe",
                                            "params": [{"mentions": [addr]}, {"commitment": "confirmed"}]}))
                        time.sleep(SUBSCRIBE_GAP)

                    while not shard.pending.empty():  # everything queued so far is covered by the full list below
                        shard.pending.get_nowait()
                    for a in sorted(shard.addrs):
                        subscribe(a)
                    shard.connected, backoff = True, 2.0
                    log.info("Websocket %d watching %d addresses", index, len(subscribed))
                    while True:
                        try:
                            while True:
                                a = shard.pending.get_nowait()
                                if a in shard.addrs and a not in subscribed:
                                    subscribe(a)
                        except queue.Empty:
                            pass
                        try:
                            msg = json.loads(ws.recv(timeout=1))
                        except TimeoutError:
                            continue
                        self._on_message(msg, req_addr, sub_addr)
            except Exception as e:  # dropped, refused, bad URL: poll until it is back, then retry
                log.warning("Websocket %d down (%s); polling until it is back", index, type(e).__name__)
            shard.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_message(self, msg: dict, req_addr: dict[int, str], sub_addr: dict[int, str]) -> None:
        if msg.get("method") == "logsNotification":
            params = msg.get("params") or {}
            v = (params.get("result") or {}).get("value") or {}
            if v.get("err") is None and v.get("signature"):
                self._on_log(sub_addr.get(params.get("subscription")), v["signature"])
            return
        rid = msg.get("id")
        if rid in req_addr:
            if "result" in msg:
                sub_addr[msg["result"]] = req_addr[rid]
            elif "error" in msg:
                if not self._rejected:
                    log.warning("The node refused a subscription (%s): that wallet is polled instead", (msg["error"] or {}).get("message"))
                self._rejected.add(req_addr[rid])

    def _on_log(self, addr: str | None, sig: str) -> None:
        """A transaction mentioning a watched address reached us (runs on a websocket thread)."""
        wallet = self.addr_to_wallet.get(addr) if addr else None
        if addr and wallet is None:
            return  # a wallet we stopped watching
        if wallet:
            now = self.clock()
            with self._guard_lock:
                if self._ignored.get(wallet, 0) > now:
                    return
                if self.max_wallet_txs:
                    q = self._guard.setdefault(wallet, deque())
                    q.append(now)
                    while q and q[0] < now - 3600:
                        q.popleft()
                    if len(q) > self.max_wallet_txs:
                        self._ignored[wallet] = now + IGNORE_SECONDS
                        self._guard.pop(wallet, None)
                        log.warning("Ignoring %s for %dh: %d transactions in an hour is a bot", wallet[:6], IGNORE_SECONDS // 3600, len(q))
                        return
            self._hint.setdefault(sig, set()).add(wallet)
        self.inbox.put(sig)

    # ---- collect new trades ----
    def _mark_seen(self, sig: str) -> bool:
        if sig in self._seen_set:
            return False
        if len(self.seen) == self.seen.maxlen:
            self._seen_set.discard(self.seen[0])
        self.seen.append(sig)
        self._seen_set.add(sig)
        return True

    def _forget(self, sig: str) -> None:
        self.retry.pop(sig, None)
        self._hint.pop(sig, None)
        self._first_seen.pop(sig, None)

    def _poll_addresses(self, due: bool) -> list[str]:
        """The addresses to read for new signatures now. A short list: all of them. A crowd: only the wallets whose
        coins we hold (plus any that fit on no websocket, or that the node refused), whatever state the streams are in."""
        if not due:
            return []
        now = self.clock()
        live = [a for a, w in self.addr_to_wallet.items() if self._ignored.get(w, 0) <= now]
        if len(self.addr_to_wallet) <= EAGER_MAX:
            return live
        return [a for a in live if self.addr_to_wallet[a] in self.priority or a in self._overflow or a in self._rejected]

    def poll(self) -> list[Swap]:
        """New trades by watched wallets, oldest first. Raises RpcError if the RPC is failing."""
        while True:  # park pushed signatures first, so a failing poll below can't lose them
            try:
                sig = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.retry.setdefault(sig, 0)
            self._first_seen.setdefault(sig, self.clock())
        ws_ok = self.ws_connected
        interval = self.poll_seconds if ws_ok else self.fallback_poll_seconds
        due = time.monotonic() - self._last_poll >= interval
        if due:
            self._last_poll = time.monotonic()
        for addr in self._poll_addresses(due):
            if addr not in self.cursor:  # an address we never read: start from now
                latest = self.rpc.signatures(addr, limit=1)
                self.cursor[addr] = latest[0]["signature"] if latest else None
                continue
            new = self.rpc.signatures(addr, limit=50, until=self.cursor.get(addr))
            if new:
                self.cursor[addr] = new[0]["signature"]
            for s in reversed(new):
                if s.get("err") is None:
                    self.retry.setdefault(s["signature"], 0)
                    self._first_seen.setdefault(s["signature"], self.clock())
                    self._hint.setdefault(s["signature"], set()).add(self.addr_to_wallet.get(addr, ""))

        out: list[Swap] = []
        if not self.retry:
            return out
        sol = self.sol_usd()  # before anything is marked seen: if the price feed is down, nothing is lost
        now = self.clock()
        while self._reads and self._reads[0] < now - 3600:
            self._reads.popleft()
        held = self.priority
        order = sorted(self.retry, key=lambda s: not (self._hint.get(s, set()) & held))  # coins we hold first, stable otherwise
        reads = 0
        for sig in order:
            if sig in self._seen_set:
                self._forget(sig)
                continue
            first = held and (self._hint.get(sig, set()) & held)
            if self.max_age and now - self._first_seen.get(sig, now) > self.max_age and not self.retry.get(sig):
                self._forget(sig)  # waited in the queue past the point where a copy would be pointless
                continue
            if self.max_reads_poll and reads >= self.max_reads_poll:
                break  # the rest wait for the next poll
            if self.max_reads_hour and len(self._reads) >= self.max_reads_hour and not first:
                self.skipped_reads += 1  # this hour's read budget is spent: only the coins we hold are still read
                self._forget(sig)
                continue
            reads += 1
            self._reads.append(now)
            try:
                tx = self.rpc.transaction(sig)
            except RpcError:
                tx = None
            if tx is None:  # not visible to this RPC node yet: try again next poll
                self.retry[sig] = self.retry.get(sig, 0) + 1
                if self.retry[sig] > 8:
                    log.warning("Gave up on transaction %s: the RPC never returned it", sig)
                    self._forget(sig)
                    self._mark_seen(sig)
                continue
            hint = self._hint.get(sig) or set()
            self._forget(sig)
            self._mark_seen(sig)
            for wallet in (hint & set(self.addr_to_wallet.values())) or dict.fromkeys(self.addr_to_wallet.values()):
                sw = parse_swap(tx, wallet, sol)
                if not sw:
                    continue
                if sw.t and self.clock() - sw.t > self.max_age:
                    log.info("Ignored a %s by %s from %ds ago: too old to copy", sw.side, wallet[:6], self.clock() - sw.t)
                    continue
                out.append(sw)
                self.last_event_at = self.clock()
        return sorted(out, key=lambda s: (s.slot, s.t))
