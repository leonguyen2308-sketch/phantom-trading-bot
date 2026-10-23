"""Watching hundreds of wallets: connection sharding, priority, the bot guard and the read budget."""

from solders.keypair import Keypair

from fomo.watcher import EAGER_MAX, LeaderWatcher
from tests.test_fomo_io import FakeRpc, buy_tx


def wallets(n):
    return [str(Keypair().pubkey()) for _ in range(n)]


class CountingRpc(FakeRpc):
    def __init__(self):
        super().__init__()
        self.sig_calls, self.tx_calls = [], []

    def signatures(self, address, limit=50, before=None, until=None):
        self.sig_calls.append(address)
        return super().signatures(address, limit, before, until)

    def transaction(self, sig):
        self.tx_calls.append(sig)
        return super().transaction(sig)


def crowd(rpc=None, **kw):
    now = [1_700_000_000]
    w = LeaderWatcher(rpc or CountingRpc(), lambda: 150.0, use_ws=False, fallback_poll_seconds=0, clock=lambda: now[0],
                      usdc_accounts=False, **kw)
    return w, now


def test_wallets_are_spread_over_the_connections_and_the_overflow_is_polled_not_dropped():
    w, _ = crowd(ws_connections=3, wallets_per_connection=40)
    ws = wallets(130)
    w.watch(set(ws))
    sizes = sorted(len(s.addrs) for s in w._shards)
    assert sizes == [40, 40, 40] and len(w._overflow) == 10  # 3 x 40 = 120 fit, ten don't


def test_wallets_we_hold_coins_from_get_a_connection_slot_first():
    w, _ = crowd(ws_connections=1, wallets_per_connection=5)
    ws = wallets(8)
    w.priority = {ws[7]}
    w.watch(set(ws))
    assert ws[7] in w._shards[0].addrs and ws[7] not in w._overflow


def test_a_crowd_does_not_read_every_wallet_at_start_or_by_polling():
    rpc = CountingRpc()
    w, _ = crowd(rpc)
    ws = wallets(EAGER_MAX + 30)
    w.priority = {ws[0]}
    w.watch(set(ws))
    assert rpc.sig_calls == [ws[0]]  # only the wallet whose coin we hold got a starting point
    rpc.sig_calls.clear()
    w.poll()
    assert set(rpc.sig_calls) == {ws[0]}  # and it is the only one read by polling


def test_a_short_list_is_still_read_in_full():
    rpc = CountingRpc()
    w, _ = crowd(rpc)
    ws = wallets(5)
    w.watch(set(ws))
    assert sorted(rpc.sig_calls) == sorted(ws)


def test_a_pushed_signature_is_parsed_for_the_wallet_whose_subscription_reported_it():
    rpc = CountingRpc()
    w, now = crowd(rpc)
    ws = wallets(EAGER_MAX + 5)
    w.watch(set(ws))
    rpc.txs["s1"] = buy_tx(ws[3], now[0])
    rpc.txs["s1"]["transaction"]["signatures"] = ["s1"]
    req, sub = {1: ws[3]}, {}
    w._on_message({"jsonrpc": "2.0", "id": 1, "result": 77}, req, sub)
    assert sub == {77: ws[3]}
    w._on_message({"method": "logsNotification", "params": {"subscription": 77, "result": {"value": {"signature": "s1", "err": None}}}}, req, sub)
    got = w.poll()
    assert [s.wallet for s in got] == [ws[3]]


def test_a_failed_transaction_is_not_read():
    w, _ = crowd()
    w._on_message({"method": "logsNotification", "params": {"subscription": 1, "result": {"value": {"signature": "bad", "err": {"x": 1}}}}}, {}, {})
    assert w.inbox.empty()


def test_a_wallet_that_trades_too_often_is_ignored_for_hours():
    w, now = crowd(max_wallet_txs_per_hour=3)
    ws = wallets(2)
    w.watch(set(ws))
    for i in range(6):
        w._on_log(ws[0], f"a{i}")
    w._on_log(ws[1], "b0")
    queued = []
    while not w.inbox.empty():
        queued.append(w.inbox.get())
    assert queued == ["a0", "a1", "a2", "b0"] and w.ignored_count == 1
    now[0] += 5 * 3600
    w._on_log(ws[0], "later")
    assert w.inbox.empty() and w.ignored_count == 1  # still ignored after five hours
    now[0] += 2 * 3600
    w._on_log(ws[0], "much-later")
    assert w.inbox.get_nowait() == "much-later"


def test_the_hourly_read_budget_spares_only_the_coins_we_hold():
    rpc = CountingRpc()
    w, now = crowd(rpc, max_reads_per_hour=2)
    ws = wallets(3)
    w.priority = {ws[2]}
    w.watch(set(ws))
    for i in range(5):
        rpc.txs[f"x{i}"] = buy_tx(ws[0], now[0])
        rpc.txs[f"x{i}"]["transaction"]["signatures"] = [f"x{i}"]
        w._on_log(ws[0], f"x{i}")
    rpc.txs["held"] = buy_tx(ws[2], now[0])
    rpc.txs["held"]["transaction"]["signatures"] = ["held"]
    w._on_log(ws[2], "held")
    w.poll()
    assert rpc.tx_calls == ["held", "x0"]  # the held coin is read first and counts toward the budget; one read is left for the rest
    assert w.skipped_reads == 4
    rpc.txs["held2"] = buy_tx(ws[2], now[0])
    rpc.txs["held2"]["transaction"]["signatures"] = ["held2"]
    w._on_log(ws[2], "held2")
    w.poll()
    assert rpc.tx_calls[-1] == "held2"  # over budget, but a trade of a coin we hold is still read


def test_reads_per_poll_are_capped_and_the_rest_wait_for_the_next_poll():
    rpc = CountingRpc()
    w, now = crowd(rpc, max_reads_per_poll=2)
    ws = wallets(2)
    w.watch(set(ws))
    for i in range(5):
        rpc.txs[f"y{i}"] = buy_tx(ws[0], now[0])
        rpc.txs[f"y{i}"]["transaction"]["signatures"] = [f"y{i}"]
        w._on_log(ws[0], f"y{i}")
    w.poll()
    assert len(rpc.tx_calls) == 2 and len(w.retry) == 3
    w.poll()
    w.poll()
    assert len(rpc.tx_calls) == 5 and not w.retry


def test_a_signature_that_waited_past_the_age_limit_is_dropped_unread():
    rpc = CountingRpc()
    ws = wallets(2)
    w2, now2 = crowd(rpc, max_age_seconds=60, max_reads_per_poll=1)
    w2.watch(set(ws))
    rpc.txs["first"] = buy_tx(ws[0], now2[0])
    rpc.txs["first"]["transaction"]["signatures"] = ["first"]
    w2._on_log(ws[0], "first")
    w2._on_log(ws[0], "waiting")
    w2.poll()  # reads "first" only
    now2[0] += 300
    w2.poll()
    assert "waiting" not in w2.retry and rpc.tx_calls == ["first"]


def test_status_says_what_is_going_on():
    w, _ = crowd(max_reads_per_hour=1000, ws_connections=2, wallets_per_connection=2)
    w.watch(set(wallets(5)))
    assert w.status() == "polling 5 wallets (no websocket)"
    w.use_ws = True  # as if the streams were configured (no thread is started in a test)
    text = w.status()
    assert "5 wallets" in text and "1 addresses over capacity" in text and "reads 0/1000" in text


# ---- the real websocket code against a local fake node ----
class FakeNode:
    """Speaks just enough of the Solana websocket protocol: logsSubscribe -> a subscription id, and pushes notifications."""

    def __init__(self, refuse=()):
        import json
        import threading
        from websockets.sync.server import serve
        self.json, self.refuse = json, set(refuse)
        self.connections = []  # one dict per client connection: {"subs": {sub_id: address}, "ws": ws}
        self.lock = threading.Lock()
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, ws):
        conn = {"subs": {}, "ws": ws}
        with self.lock:
            self.connections.append(conn)
        try:
            for raw in ws:
                msg = self.json.loads(raw)
                if msg.get("method") != "logsSubscribe":
                    continue
                addr = msg["params"][0]["mentions"][0]
                if addr in self.refuse:
                    ws.send(self.json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "too many subscriptions"}}))
                    continue
                sub_id = 1000 * len(self.connections) + len(conn["subs"]) + 1
                conn["subs"][sub_id] = addr
                ws.send(self.json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": sub_id}))
        except Exception:
            pass

    def push(self, addr, sig, err=None):
        for conn in list(self.connections):
            for sub_id, a in conn["subs"].items():
                if a == addr:
                    conn["ws"].send(self.json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                        "subscription": sub_id, "result": {"context": {"slot": 1}, "value": {"signature": sig, "err": err, "logs": []}}}}))
                    return True
        return False

    def subscribed(self):
        return {a for c in self.connections for a in c["subs"].values()}

    def per_connection(self):
        return sorted(len(c["subs"]) for c in self.connections)

    def close(self):
        self.server.shutdown()


def wait_for(cond, seconds=8.0):
    import time
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def live_watcher(node, **kw):
    now = [1_700_000_000]
    w = LeaderWatcher(CountingRpc(), lambda: 150.0, ws_url=f"ws://127.0.0.1:{node.port}", clock=lambda: now[0], usdc_accounts=False, **kw)
    return w, now


def test_real_websockets_share_the_wallets_and_push_trades_to_the_right_wallet():
    node = FakeNode()
    try:
        w, now = live_watcher(node, ws_connections=2, wallets_per_connection=3)
        ws = wallets(6)
        w.watch(set(ws))
        assert wait_for(lambda: node.subscribed() == set(ws) and w.ws_connected)
        assert node.per_connection() == [3, 3]
        rpc = w.rpc
        rpc.txs["live1"] = buy_tx(ws[4], now[0])
        rpc.txs["live1"]["transaction"]["signatures"] = ["live1"]
        assert node.push(ws[4], "live1")
        assert wait_for(lambda: not w.inbox.empty())
        got = w.poll()
        assert [(s.wallet, s.side) for s in got] == [(ws[4], "buy")] and rpc.tx_calls == ["live1"]
        node.push(ws[1], "failed-tx", err={"InstructionError": [0, "x"]})
        import time
        time.sleep(0.3)
        assert w.inbox.empty()  # a failed transaction is not a trade
        assert "websockets up" in w.status() and "6 wallets" in w.status()
    finally:
        node.close()


def test_a_wallet_added_later_is_subscribed_on_the_live_connection():
    node = FakeNode()
    try:
        w, _ = live_watcher(node, ws_connections=1, wallets_per_connection=10)
        first = wallets(3)
        w.watch(set(first))
        assert wait_for(lambda: node.subscribed() == set(first))
        more = wallets(2)
        w.watch(set(first) | set(more))
        assert wait_for(lambda: node.subscribed() == set(first) | set(more))
        assert len(node.connections) == 1  # no reconnect for a new wallet
    finally:
        node.close()


def test_a_subscription_the_node_refuses_is_polled_instead():
    ws = wallets(3)
    node = FakeNode(refuse={ws[2]})
    try:
        w, _ = live_watcher(node, ws_connections=1, wallets_per_connection=10)
        w.watch(set(ws))
        assert wait_for(lambda: w._rejected == {ws[2]} and node.subscribed() == {ws[0], ws[1]})
        assert "1 refused by the node" in w.status()
    finally:
        node.close()


def test_a_dropped_connection_comes_back_and_resubscribes():
    node = FakeNode()
    try:
        w, _ = live_watcher(node, ws_connections=1, wallets_per_connection=10)
        ws = wallets(3)
        w.watch(set(ws))
        assert wait_for(lambda: node.subscribed() == set(ws) and w.ws_connected)
        node.connections[0]["ws"].close()
        assert wait_for(lambda: not w.ws_connected, 5)
        assert wait_for(lambda: len(node.connections) == 2 and w.ws_connected, 15)  # backs off 2 s, then reconnects
        assert set(node.connections[1]["subs"].values()) == set(ws)
    finally:
        node.close()
