"""The desk.

SCANNER (its own thread; slow work): sweep the universe -> G1 market -> G2 on-chain -> G3 quote -> G4 candles ->
exit plan -> shadow position -> Claude seats -> CHIEF (code) -> buy.
MANAGER (main thread, every second): prices every 10 s for open positions (real and shadow), the exit plan,
equity, hard limits, the kill switch. A slow desk review never delays a stop.

Locking: `self.lock` guards the journal's view of positions; swaps and alerts run outside it, so a slow swap on
one thread never freezes the other thread's stops. A position being sold is marked in flight so two threads can't
sell it twice.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from desk import funnel
from desk.ai import DeskUnavailable, sanitize_symbol
from desk.features import LEGEND, candle_fields, market_fields, snapshot
from desk.journal import day_of, trade_stats
from desk.plan import ExitPlan, make_plan, step as plan_step
from desk.sources import _num
from fomo.brokers import Fill
from fomo.swaps import USDC
from phantom.execution.jupiter import SwapOutcomeUnknown

log = logging.getLogger(__name__)

PRICE_SECONDS = 10
VOLUME_SECONDS = 300
MARK_SECONDS = 15
DUST_USD = 0.5
WRITE_OFF_AFTER = 30  # failed sells of one position before it is written off (if worth little) or retried slowly
SLOW_RETRY_SECONDS = 60
SETTLE_SECONDS = 180  # a swap whose outcome was unknown and that hasn't shown up on chain by now never will
SETTLE_GIVE_UP_SECONDS = 360  # history still unreadable this long after: settle from the wallet balance instead
STREAK_TTL_SECONDS = 1800  # error streaks saved more than this long ago are forgotten at start-up
UNPRICED_SECONDS = 120  # no price from any source for this long: the coin is treated as worth nothing
SHORT_READS = 3  # wallet reads in a row that must agree before the bot believes tokens left outside it
RECONCILE_GRACE_SECONDS = 120  # a fresh buy can take this long to show in RPC balances
ALERT_REPEAT_SECONDS = 300
SEATS = ("SCOUT", "ANALYST", "NEWS", "SKEPTIC", "CHIEF")
PENDING = object()  # _execute: sent, outcome unknown
# how long a sighting on a list is remembered, per fact: a rank goes stale in minutes, "newly listed" stays true for the
# hour (it must outlast the minimum age, or no coin could ever be both new and old enough)
ATTENTION_TTL = {"trending_rank_gecko": 480, "trending_rank_jupiter": 240, "new_listing": 3600}
# Data sources: when ten calls in a row fail (about 2.5 minutes), buying pauses and an alert goes out, and buying
# resumes by itself when they work again. The kill switch is NOT used for these. A network or API outage can't be
# helped by selling (the sells need the same network), a stop sells on the first fresh price anyway once the
# connection is back, and a kill at that moment would only dump every coin, winners included, at the worst time.
# The kill switch is for things that are actually broken: repeated failing buys (wallet or keys) and the loop crashing.
PAUSE_ONLY_SOURCES = ("scan", "jupiter", "dexscreener", "rpc", "watch", "prices", "pricing", "wallet", "settle")


def _why(e: Exception) -> str:
    """What to show for a failed request: the HTTP status when there is one (401 = a bad key, 429 = too many requests)."""
    code = getattr(getattr(e, "response", None), "status_code", None)
    return f"HTTP {code}" if code else type(e).__name__


def _no_route(e: Exception) -> bool:
    """Jupiter answered that nobody will take the other side, as opposed to any other failure."""
    return isinstance(e, ValueError) and "no route" in str(e).lower()


def _load_errors(saved, now: float) -> dict[str, int]:
    if isinstance(saved, dict) and "errors" in saved:
        return {} if now - float(saved.get("t") or 0) > STREAK_TTL_SECONDS else {k: int(v) for k, v in saved["errors"].items()}
    if isinstance(saved, dict):
        return {k: int(v) for k, v in saved.items()}
    return {"general": int(saved or 0)} if saved else {}


class Desk:
    def __init__(self, *, mode: str, cfg, broker, jup, dex, gecko, rug, holders, mint_info, prices, desk, journal, kill,
                 alerts=None, watcher=None, tracked: dict | None = None, clock=time.time, threaded: bool = True, mdex=None):
        self.mode, self.cfg, self.broker = mode, cfg, broker
        self.jup, self.dex, self.gecko, self.rug, self.holders, self.mint_info_fn, self.prices = jup, dex, gecko, rug, holders, mint_info, prices
        self.mdex = mdex or dex  # the manager's DexScreener (own HTTP client, short 429 waits)
        self.desk, self.journal, self.kill, self.alerts, self.clock = desk, journal, kill, alerts, clock
        self.watcher, self.tracked = watcher, tracked or {}
        self.threaded = threaded
        self.lock = threading.RLock()
        self.queue: queue.Queue = queue.Queue()
        self.errors: dict[str, int] = _load_errors(journal.get(f"api_errors:{mode}", 0), clock())
        self._reset_seen = journal.get(f"reset_at:{mode}")
        self._error_window: dict[str, float] = {}
        self._alerted: dict[str, float] = {}
        self._mints: dict[str, object] = {}
        self._cool: dict[str, float] = {}
        self._counted: dict[tuple[str, str], set] = {}
        self._reviews: deque[float] = deque()
        self._sell_failures: dict[int, int] = {}
        self._retry_at: dict[int, float] = {}
        self._empty_reads: dict[int, int] = {}
        self._short_reads: dict[int, int] = {}
        self._inflight: set[int] = set()  # positions whose sell is running
        self._queued: set[int] = set()  # positions whose sell is waiting for a worker
        self._executing: set[int] = set()  # pending buy rows whose swap is being sent
        self._attn: dict[str, dict] = {}  # mint -> attention numbers + when seen
        self._pools: dict[str, str] = {}  # mint -> pool address from GeckoTerminal, when DexScreener has none
        self._dead_lists: dict[str, float] = {}  # extra Jupiter lists the server says do not exist -> ask again after
        self._no_route_n: dict[int, int] = {}
        self._unpriced_n: dict[str, int] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sell") if threaded else None
        self._book_version = 0  # bumped whenever a position is opened or an exit booked (see mark)
        self._breaches: dict[str, int] = {}  # limit breaches seen in consecutive marks
        self.sell_only: str | None = None  # set when live trading may only sell (go-live checks no longer pass)
        self.draining = False  # shutting down: no new buys
        self._alert_q: queue.Queue = queue.Queue()
        self._alert_thread: threading.Thread | None = None
        self.prices_now: dict[str, float] = {}
        self.price_t: dict[str, float] = {}  # when each price was last confirmed by a source
        self.unpriced: dict[str, float] = {}  # since when every source answered without a price for the mint
        self.vol_now: dict[str, float] = {}
        self.equity_now = float(journal.get(f"equity:{mode}", 0.0) or 0.0)
        self.sol_now: float | None = None
        self.sweeps = 0
        self.last = {"sweep": -1e18, "price": -1e18, "volume": -1e18, "mark": -1e18}
        self.scan_state = "idle"
        self.latest: dict | None = journal.get(f"latest:{mode}")
        self._stop = threading.Event()

    # ================================================================ plumbing
    def _feed(self, kind: str, seat: str, text: str, pnl: float | None = None, alert: bool = False) -> None:
        with self.lock:
            self.journal.event(self.mode, self.clock(), kind, seat, text, pnl)
        log.info("[%s/%s] %s%s", seat, kind, text, f" ({pnl:+.2f})" if pnl is not None else "")
        if alert:
            self._alert(f"{seat}: {text}" + (f"  {pnl:+.2f} USD" if pnl is not None else ""))

    def _alert(self, text: str) -> None:
        key = text[:60]
        if time.monotonic() - self._alerted.get(key, -1e9) < ALERT_REPEAT_SECONDS:
            return
        self._alerted[key] = time.monotonic()
        if not self.alerts:
            return
        if not self.threaded:
            return self._send_alert(f"[{self.mode.upper()}] {text}")
        self._alert_q.put(f"[{self.mode.upper()}] {text}")  # sent by a worker: Telegram can take 30 s, trading can't wait
        if self._alert_thread is None or not self._alert_thread.is_alive():
            self._alert_thread = threading.Thread(target=self._alert_loop, daemon=True, name="alerts")
            self._alert_thread.start()

    def _alert_loop(self) -> None:
        while True:
            self._send_alert(self._alert_q.get())

    def _send_alert(self, text: str) -> None:
        try:
            self.alerts.send(text)
        except Exception as e:  # alerts never break trading
            log.warning("alert failed: %s", type(e).__name__)

    @property
    def api_errors(self) -> int:
        return max(self.errors.values(), default=0)

    def _api_error(self, what: str, source: str = "general") -> None:
        """Counted once per 15 s per source, so a short outage can't fire the kill switch (~2.5 min of failure can)."""
        now = self.clock()
        if now - self._error_window.get(source, -1e18) < MARK_SECONDS:
            log.warning("API error (%s, same window): %s", source, what)
            return
        self._error_window[source] = now
        self.errors[source] = self.errors.get(source, 0) + 1
        self._save_errors()
        log.warning("API error %d (%s): %s", self.errors[source], source, what)
        limit = self.cfg.risk.max_consecutive_api_errors
        if source in PAUSE_ONLY_SOURCES:
            if self.errors[source] == limit:
                self._feed("STOP", "CHIEF", f"{source} failed {limit} times in a row ({what}): no new buys until it works again; "
                                            f"coins already held keep being managed", alert=True)
        elif self.errors[source] >= limit and not self.kill.tripped:
            self.trip(f"{self.errors[source]} API errors in a row ({source}; last: {what})")

    def _api_ok(self, source: str) -> None:
        if self.errors.get(source):
            self.errors[source] = 0
            self._save_errors()

    def _save_errors(self) -> None:
        """Per source, so a restart in the middle of an outage keeps counting where it was (and nothing else does)."""
        self.journal.put(f"api_errors:{self.mode}", {"t": self.clock(), "errors": {k: v for k, v in self.errors.items() if v}})

    def _count(self, gate: str, mint: str) -> None:
        day = day_of(self.clock())
        s = self._counted.setdefault((day, gate), set())
        if mint not in s:
            s.add(mint)
            with self.lock:
                self.journal.count(self.mode, self.clock(), gate)

    def _kill(self, gate: str, mint: str, why: list[str], cool: bool) -> None:
        day = day_of(self.clock())
        s = self._counted.setdefault((day, "kill:" + gate), set())
        if mint not in s:  # each token's reasons are counted once a day per gate, not on every sweep
            s.add(mint)
            with self.lock:
                self.journal.kill(self.mode, self.clock(), gate, why)
        if cool:
            self._cool[mint] = self.clock() + self.cfg.scan.recheck_killed_minutes * 60

    def _mint_info(self, mint: str):
        """Cached only once both authorities are revoked (that can't be undone); otherwise read again on every look,
        so a coin whose creator revokes them later isn't killed on a stale answer."""
        if mint in self._mints:
            return self._mints[mint]
        info = self.mint_info_fn(mint)
        if not info.mint_authority and not info.freeze_authority:
            self._mints[mint] = info
        return info

    # ================================================================ the scanner
    def start(self) -> None:
        if self.threaded:
            threading.Thread(target=self._scan_loop, daemon=True, name="scanner").start()

    def stop(self) -> None:
        self._stop.set()

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
                self._api_ok("scan")
            except Exception as e:
                log.exception("scanner error")
                self._api_error(f"scanner: {type(e).__name__}: {e}", "scan")
            self._stop.wait(1.0)

    def scan_once(self) -> None:
        """Handle queued TRACK signals first, then a sweep if one is due."""
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            self._evaluate_mint(item["mint"], "track", source_wallet=item.get("wallet"))
        if self.clock() - self.last["sweep"] >= self.cfg.scan.interval_seconds:
            self.last["sweep"] = self.clock()
            self.sweep()

    def _buys_blocked(self) -> str | None:
        if self.kill.tripped:
            return "kill switch is on"
        if self.sell_only:
            return self.sell_only
        down = sorted(s for s in PAUSE_ONLY_SOURCES if self.errors.get(s, 0) >= self.cfg.risk.max_consecutive_api_errors)
        if down:
            return f"{', '.join(down)} keeps failing (buying resumes by itself when it works)"
        if self.draining:
            return "shutting down"
        today = day_of(self.clock())
        if self.journal.get(f"daily_stop:{self.mode}") == today:
            return "daily loss limit hit"
        until = float(self.journal.get(f"paused_until:{self.mode}", 0) or 0)
        if until > self.clock():
            return f"paused after {self.cfg.risk.pause_after_losses} losses in a row"
        if self.mode == "live" and self.sol_now is not None and self.sol_now < self.cfg.risk.min_sol_for_fees:
            return "not enough SOL for fees"
        return None

    def sweep(self) -> None:
        now, sc = self.clock(), self.cfg.scan
        self.sweeps += 1
        self.scan_state = "sweeping"
        universe: dict[str, dict | None] = {}
        attn: dict[str, dict] = {}  # numbers about attention per coin, from where it was found
        lists = [("toptrending", "5m")]
        if self.sweeps % max(sc.organic_every, 1) == 1 or sc.organic_every <= 1:
            lists.append(("toporganicscore", "1h"))
        for kind, window in lists:  # the main lists: a failure here counts toward the data-source pause
            try:
                for i, t in enumerate(self.jup.top(kind, window, 100)):
                    universe.setdefault(t.get("id"), t)
                    if kind == "toptrending":
                        self._rank(attn, t.get("id"), "trending_rank_jupiter", i + 1)
                self._api_ok("jupiter")
            except Exception as e:
                self._api_error(f"Jupiter {kind}: {_why(e)}", "jupiter")
        if self.sweeps % max(sc.extra_lists_every, 1) == 1 or sc.extra_lists_every <= 1:
            for spec in sc.extra_jupiter_lists:  # extra ways to find coins: best effort, never an error
                kind, _, window = str(spec).partition("/")
                if self._dead_lists.get(spec, 0) > now:
                    continue
                try:
                    for i, t in enumerate(self.jup.top(kind, window or "1h", 100)):
                        universe.setdefault(t.get("id"), t)
                        if kind == "toptrending":
                            self._rank(attn, t.get("id"), "trending_rank_jupiter", i + 1)
                except Exception as e:
                    log.info("Jupiter %s unavailable: %s", spec, _why(e))
                    if getattr(getattr(e, "response", None), "status_code", None) in (400, 404):
                        self._dead_lists[spec] = now + 3600  # no such list: stop asking for an hour
        try:
            for t in self.jup.recent():
                universe.setdefault(t.get("id"), t)
        except Exception as e:
            log.info("Jupiter recent unavailable: %s", type(e).__name__)
        if self.sweeps % max(sc.dexscreener_every, 1) == 1 or sc.dexscreener_every <= 1:
            for m in self.dex.discovered():
                universe.setdefault(m, None)
        pools = getattr(self.gecko, "pools", None)
        if pools and sc.gecko_discovery_every > 0 and self.sweeps % sc.gecko_discovery_every == 1 % sc.gecko_discovery_every:
            for kind, field in (("trending_pools", "trending_rank_gecko"), ("new_pools", "new_listing")):
                try:
                    for p in pools(kind):
                        universe.setdefault(p["mint"], None)
                        self._rank(attn, p["mint"], field, p["rank"] if field != "new_listing" else 1)
                        if p.get("pool"):
                            self._pools.setdefault(p["mint"], p["pool"])
                except Exception as e:
                    log.info("GeckoTerminal %s unavailable: %s", kind, _why(e))
        for m, a in attn.items():  # remembered per field: lists are pulled every few sweeps, and each fact ages on its own
            cur = self._attn.setdefault(m, {})
            for f, v in a.items():
                cur[f] = (v, now)
        for m in list(self._attn):
            cur = self._attn[m]
            for f in [f for f, (_, t) in cur.items() if now - t > ATTENTION_TTL.get(f, 300)]:
                del cur[f]
            if not cur:
                del self._attn[m]
        if len(self._pools) > 2000:
            self._pools.clear()
        universe.pop(None, None)
        universe.pop(USDC, None)
        mints = sorted(universe, key=lambda m: m not in attn)[: sc.max_universe]  # coins people are looking at survive the cap
        if not mints:
            self.scan_state = "idle"
            return
        missing = [m for m in mints if not universe[m]]
        if missing:
            try:
                universe.update(self.jup.search(missing))
            except Exception as e:
                log.info("Jupiter search failed: %s", type(e).__name__)
        try:
            pairs = self.dex.tokens(mints)
            self._api_ok("dexscreener")
        except Exception as e:
            self._api_error(f"DexScreener: {_why(e)}", "dexscreener")
            self.scan_state = "idle"
            return
        survivors = []
        for m in mints:
            self._count("universe", m)
            jt, dx = universe.get(m), pairs.get(m)
            mf = {**market_fields(jt, dx, now), **self._attention(m, now)}
            if self.cfg.hot.enabled:  # hot mode: market-cap band + attention, nothing else
                why = funnel.g1_hot(mf, (dx or {}).get("dexId", ""), self.cfg.hot)
            else:
                why = funnel.g1_market(mf, (dx or {}).get("dexId", ""), jt, self.cfg.gates)
            if why:
                self._kill("market", m, why, cool=False)
                continue
            self._count("market", m)
            survivors.append((m, jt, dx, mf))
        held = {p["mint"] for p in self.journal.open_positions(self.mode)} | {r["mint"] for r in self.journal.pending(self.mode)}
        ready = [s for s in survivors if self._cool.get(s[0], 0) <= now and s[0] not in held]
        ready.sort(key=lambda s: self._priority(s[3]), reverse=True)
        checking = min(len(ready), sc.deep_checks_per_sweep)
        if checking or self.sweeps % 15 == 1:
            self._feed("SCAN", "SCOUT", f"swept {len(mints)} tokens · {len(survivors)} pass the market gate · checking {checking} more closely")
        started = self.clock()
        if ready and (self._buys_blocked() or not self.ticket_usd()[0]):
            ready = []  # no buy is possible right now: don't spend RPC calls and a SKIP line on every coin
        for m, jt, dx, mf in ready[: sc.deep_checks_per_sweep]:
            if self._stop.is_set() or self.clock() - started > 0.8 * sc.interval_seconds:
                break  # the rest wait for the next sweep, in priority order
            self.evaluate(m, "scout", jt, dx, mf)
        self.scan_state = "idle"

    @staticmethod
    def _rank(attn: dict, mint, field: str, rank: int) -> None:
        if mint:
            a = attn.setdefault(mint, {})
            a[field] = min(rank, a.get(field, rank)) if field != "new_listing" else 1

    def _attention(self, mint: str, now: float) -> dict:
        """What is known about how much attention a coin gets (numbers only), kept for 15 minutes after it was seen on a list."""
        seen = {f: v for f, (v, t) in self._attn.get(mint, {}).items() if now - t <= ATTENTION_TTL.get(f, 300)}
        return {"trending_rank_gecko": None, "trending_rank_jupiter": None, "new_listing": 0, **seen}

    @staticmethod
    def _priority(mf: dict) -> float:
        """Who gets the deep checks first when there are more finalists than slots: the most active coins, and those
        more people are looking at (trending on GeckoTerminal or Jupiter, or newly listed). Order only: it never lets a
        coin past a gate."""
        score = (mf.get("vol_accel") or 1) * (mf.get("organic_score") or 50)
        if mf.get("trending_rank_gecko"):
            score *= 1.5
        if mf.get("trending_rank_jupiter") and mf["trending_rank_jupiter"] <= 30:
            score *= 1.2
        if mf.get("new_listing"):
            score *= 1.2
        return score

    def _evaluate_mint(self, mint: str, station: str, source_wallet: str | None = None) -> None:
        """A TRACK signal: fetch the market numbers for one mint, then run the same funnel."""
        now = self.clock()
        if self._cool.get(mint, 0) > now or any(p["mint"] == mint for p in [*self.journal.open_positions(self.mode), *self.journal.pending(self.mode)]):
            return
        try:
            jt = self.jup.search([mint]).get(mint)
            dx = self.dex.tokens([mint]).get(mint)
        except Exception as e:
            self._api_error(f"track lookup: {type(e).__name__}", "scan")
            return
        self._count("universe", mint)
        mf = market_fields(jt, dx, now)
        why = funnel.g1_market(mf, (dx or {}).get("dexId", ""), jt, self.cfg.gates)
        if why:
            self._kill("market", mint, why, cool=False)
            self._feed("SKIP", "SCOUT", f"tracked wallet bought ${(jt or {}).get('symbol', mint[:4])}: {why[0]}")
            return
        self._count("market", mint)
        self.evaluate(mint, station, jt, dx, mf, source_wallet=source_wallet)

    # ---------------------------------------------------------------- one candidate
    def ticket_usd(self) -> tuple[float, str]:
        r = self.cfg.risk
        try:
            cash = self.broker.usdc()
        except Exception as e:
            return 0.0, f"wallet unreadable ({type(e).__name__})"
        with self.lock:
            opens = self.journal.open_positions(self.mode)
            pend = [x for x in self.journal.pending(self.mode) if x["side"] == "buy"]
        if len(opens) + len(pend) >= r.max_open_positions:
            return 0.0, f"already {len(opens) + len(pend)} open positions"
        eq = self.equity_now or cash
        open_cost = sum(p["cost_usd"] * (p["raw"] - p["sold_raw"]) / p["raw"] for p in opens if p["raw"]) + sum(x["usd"] for x in pend)
        size = min(eq * r.position_pct / 100, r.max_position_usd, eq * r.max_exposure_pct / 100 - open_cost, cash)
        if size < r.min_position_usd:
            return 0.0, f"ticket ${max(size, 0):.2f} under the ${r.min_position_usd:g} minimum"
        return round(size, 2), ""

    def evaluate(self, mint: str, station: str, jt: dict | None, dx: dict | None, mf: dict, source_wallet: str | None = None) -> str:
        """Run one token through G2..CHIEF. Returns the outcome word, for tests and logs."""
        now, cfg = self.clock(), self.cfg
        symbol = sanitize_symbol((jt or {}).get("symbol") or ((dx or {}).get("baseToken") or {}).get("symbol") or mint[:4]) or mint[:4]
        blocked = self._buys_blocked()
        if blocked:
            return "blocked"
        ticket, why_not = self.ticket_usd()
        if not ticket:
            self._feed("SKIP", "CHIEF", f"${symbol} passed the market gate, but {why_not}")
            return "no-room"

        # G2: on-chain facts, cheapest first. A coin that dies on its authorities costs one RPC call, on its holders
        # about four, and only a survivor pays for the ten wallet-age lookups (the free RPC plan is 1M calls a month).
        def die(why: list[str]) -> str:
            self._kill("onchain", mint, why, cool=True)
            self._feed("KILL", "SCOUT", f"${symbol} · on-chain: {'; '.join(why[:2])}")
            return "onchain"

        def rpc_error(e: Exception) -> str:  # set aside: one coin the node can't read must not build an error streak alone
            self._cool[mint] = now + cfg.scan.recheck_passed_minutes * 60
            self._api_error(f"RPC: {type(e).__name__}: {e}", "rpc")
            return "error"
        hot = cfg.hot.enabled
        rules = cfg.holders
        if hot:  # hot mode asks for revoked authorities only if configured; Token-2022 traps are always refused
            rules = dataclasses.replace(cfg.holders, require_mint_authority_revoked=cfg.hot.require_authorities_revoked,
                                        require_freeze_authority_revoked=cfg.hot.require_authorities_revoked)
        try:
            info = self._mint_info(mint)
        except Exception as e:
            return rpc_error(e)
        if why := funnel.g2_onchain(info, {}, None, rules):  # mint or freeze authority, Token-2022 extensions
            return die(why)
        if hot:  # no holder, wallet-age or RugCheck lookups: no RPC calls beyond the authorities, and no unknowns to wait for
            hf, rug = {"top1_pct": None, "top10_pct": None, "fresh_share_pct": None, "rugcheck_risk": None}, None
            self._count("onchain", mint)
        else:
            try:
                top = self.holders.top(mint, pools=(dx or {}).get("_pair_addresses") or ())
            except Exception as e:
                return rpc_error(e)
            hf = {"top1_pct": round(top[0][1], 3) if top else None, "top10_pct": round(sum(p for _, p in top[:10]), 3) if top else None,
                  "fresh_share_pct": None, "rugcheck_risk": None}
            if why := funnel.g2_onchain(info, hf, None, cfg.holders):  # concentration
                return die(why)
            rug = self.rug.summary(mint)
            hf["rugcheck_risk"] = _num(rug.get("score_normalised")) if rug else None
            if why := funnel.g2_onchain(info, hf, rug, cfg.holders):  # RugCheck
                return die(why)
            try:
                fresh, _ = self.holders.fresh_share(top, cfg.holders.fresh_wallet_hours) if top else (None, 0)
                self._api_ok("rpc")
            except Exception as e:
                return rpc_error(e)
            hf["fresh_share_pct"] = round(fresh, 3) if fresh is not None else None
            why = funnel.g2_onchain(info, hf, rug, cfg.holders)
            if why:
                return die(why)
            unknown = funnel.g2_unknowns(hf, rug)
            if unknown and (reason := getattr(self.rug, "last_error", {}).get(mint)):
                unknown = [u.replace("(unknown is not a pass)", f"({reason}; unknown is not a pass)") for u in unknown]
            if unknown:  # can't see it, so can't buy it; looked at again later, not banned for hours
                self._kill("onchain", mint, unknown, cool=False)
                self._cool[mint] = now + cfg.scan.recheck_passed_minutes * 60
                self._feed("KILL", "SCOUT", f"${symbol} · on-chain: {'; '.join(unknown)}")
                return "onchain"
            self._count("onchain", mint)

        # G3: a real quote for our ticket, and selling it straight back
        try:
            q = self.broker.quote_buy(mint, ticket)
        except ValueError as e:
            self._kill("depth", mint, [f"no buy route ({e})"], cool=True)
            return "depth"
        except Exception as e:
            self._api_error(f"quote: {_why(e)}", "jupiter")
            return "error"
        try:
            back = self.broker.quote_sell(mint, q.raw)
        except ValueError:
            back = None
        except Exception as e:
            self._api_error(f"quote: {_why(e)}", "jupiter")
            return "error"
        self._api_ok("jupiter")
        depth = cfg.depth if not hot else dataclasses.replace(
            cfg.depth, max_price_impact_pct=cfg.hot.max_price_impact_pct, max_round_trip_loss_pct=cfg.hot.max_round_trip_loss_pct,
            max_slippage_tolerance_pct=cfg.hot.max_slippage_tolerance_pct)
        df, why = funnel.g3_depth(q, back, info.decimals, depth)
        if why:
            self._kill("depth", mint, why, cool=True)
            self._feed("KILL", "SCOUT", f"${symbol} · depth: {'; '.join(why[:2])}")
            return "depth"
        self._count("depth", mint)

        # G4: where it is in its move
        pool = (dx or {}).get("pairAddress") or ((jt or {}).get("firstPool") or {}).get("id") or self._pools.get(mint)
        candles = []
        if pool:
            try:
                candles = self.gecko.candles(pool, 5, 72, token=mint)
            except Exception as e:  # GeckoTerminal throttled or down: not a verdict on the coin, so no window kill
                log.info("candles unavailable for %s: %s", symbol, _why(e))
                if not hot:
                    self._cool[mint] = now + 120
                    return "error"
        cf = candle_fields(candles, now)
        why = [] if hot else funnel.g4_window(cf, len(candles), cfg.window)  # hot mode: no verdict on the chart
        entry = q.usd / (q.raw / 10 ** info.decimals) if q.raw else 0.0
        plan, plan_why = make_plan(entry, cf.get("atr_5m_pct"), mf.get("vol_5m_usd") or 0.0, cfg.plan, strict=not hot)
        if why or not plan:
            self._kill("window", mint, why + plan_why, cool=False)
            self._cool[mint] = now + cfg.scan.recheck_passed_minutes * 60
            self._feed("KILL", "SCOUT", f"${symbol} · entry window: {'; '.join((why + plan_why)[:2])}")
            return "window"
        self._count("window", mint)
        snap = snapshot(mf, cf, hf, df)
        checks = funnel.checks(snap, {})
        plan_view = {"entry": entry, "stop_pct": round(plan.stop_pct, 2),
                     "take_profits_pct": list(cfg.plan.ladder_pct), "take_profit_share_each": cfg.plan.ladder_fraction,
                     "trail_pct_after_first_take_profit": cfg.plan.trail_pct, "book_pct": cfg.plan.book_pct,
                     "round_trip_cost_pct": cfg.plan.round_trip_cost_pct, "max_hold_hours": cfg.plan.max_hold_hours}
        cap = snap.get("mcap_usd") or 0
        why_hot = f" · {', '.join(funnel.attention_signals(mf, cfg.hot))}" if hot else ""
        self._feed("PICK", "SCOUT", f"${symbol} passed every gate ({station}) · mcap {f'${cap / 1e6:.2f}M' if cap >= 1e6 else f'${cap / 1e3:.0f}K'} · "
                                    f"liq ${(snap.get('liquidity_usd') or 0) / 1e3:.0f}K · stop -{plan.stop_pct:.0f}%{why_hot}")

        # the desk
        opinion, desk_note, busy = None, "", False
        self._cool[mint] = now + cfg.scan.recheck_passed_minutes * 60
        while self._reviews and self._reviews[0] < now - 3600:
            self._reviews.popleft()
        if len(self._reviews) >= cfg.desk.max_reviews_per_hour:
            desk_note, busy = f"desk busy ({cfg.desk.max_reviews_per_hour} reviews this hour)", True
        else:
            self._reviews.append(now)
            links = [w.get("url") for w in ((dx or {}).get("info") or {}).get("websites") or [] if isinstance(w, dict)]
            links += [s.get("url") for s in ((dx or {}).get("info") or {}).get("socials") or [] if isinstance(s, dict)]
            try:
                self.scan_state = f"desk reviewing ${symbol}"
                opinion = self.desk.review(LEGEND, snap, plan_view, symbol=symbol, mint=mint, links=[u for u in links if u])
                self._api_ok("desk")
            except DeskUnavailable as e:
                desk_note = str(e)
            finally:
                self.scan_state = "sweeping"
        if opinion:
            self._feed("NOTE", "ANALYST", f"${symbol}: {opinion.setup} · {opinion.entry_window} · conviction {opinion.conviction:.2f} · {opinion.thesis}")
            if opinion.news:
                self._feed("NOTE", "NEWS", f"${symbol}: {opinion.news[:300]}")
            self._feed("NOTE", "SKEPTIC", f"${symbol}: {opinion.verdict.upper()} · {opinion.objection}")

        # CHIEF: code decides
        reasons: list[str] = []
        mult = 1.0
        if opinion is None:
            if cfg.desk.when_unavailable == "rules_only" and not busy:  # rules alone only when the desk is really down
                desk_note = (desk_note + " · trading on rules alone").strip(" ·")
            else:
                reasons.append(f"no desk opinion ({desk_note or 'unavailable'})")
        else:
            if opinion.setup not in cfg.desk.allowed_setups:
                reasons.append(f"analyst sees {opinion.setup}")
            if opinion.entry_window not in cfg.desk.allowed_windows:
                reasons.append(f"entry window {opinion.entry_window}")
            if opinion.conviction < cfg.desk.min_conviction:
                reasons.append(f"conviction {opinion.conviction:.2f} < {cfg.desk.min_conviction:g}")
            if opinion.verdict == "block":
                reasons.append("skeptic blocked it")
            mult = opinion.size_multiplier
        size = round(ticket * mult, 2)
        if not reasons and size < cfg.risk.min_position_usd:
            reasons.append(f"skeptic shrank it to ${size:.2f}, under the minimum")
        action = "skip" if reasons else "buy"
        with self.lock:
            did = self.journal.decision(mode=self.mode, t=int(now), mint=mint, symbol=symbol, station=station, snapshot=snap,
                                        checks=checks, opinion=opinion.to_dict() if opinion else {"unavailable": desk_note},
                                        verdict=opinion.verdict if opinion else None, conviction=opinion.conviction if opinion else None,
                                        action=action, note="; ".join(reasons) or desk_note, size_usd=size if action == "buy" else 0.0)
            # the rules-only arm: every token that passed the code gates, whatever the desk said
            self._open_shadow(mint, symbol, info.decimals, station, pool, ticket, entry, plan, did, df.get("impact_pct") or 0.0)
            self.latest = {"t": int(now), "mint": mint, "symbol": symbol, "station": station, "snapshot": snap, "checks": checks,
                           "candles": candles[-48:], "plan": plan.to_dict(), "opinion": opinion.to_dict() if opinion else None,
                           "action": action, "reasons": reasons, "desk_note": desk_note}
            self.journal.put(f"latest:{self.mode}", self.latest)
        if action == "skip":
            self._feed("SKIP", "CHIEF", f"${symbol} not bought: {'; '.join(reasons)}")
            return "skip"
        self._count("desk", mint)
        return self._buy(mint, symbol, info.decimals, station, pool, size, entry, cf.get("atr_5m_pct"), mf.get("vol_5m_usd") or 0.0,
                         did, source_wallet)

    # ---------------------------------------------------------------- buying
    def _buy(self, mint, symbol, decimals, station, pool, size, decided_price, atr, vol5, did, source_wallet) -> str:
        try:
            q = self.broker.quote_buy(mint, size)
        except Exception as e:
            self._feed("SKIP", "CHIEF", f"${symbol}: re-quote failed ({type(e).__name__})")
            return "error"
        price = q.usd / (q.raw / 10 ** decimals) if q.raw else 0.0
        moved = (price / decided_price - 1) * 100 if decided_price else 0.0
        if abs(moved) > self.cfg.depth.max_requote_move_pct:  # up: chasing; down: someone is dumping into the pool
            self._feed("SKIP", "CHIEF", f"${symbol}: price {'ran' if moved > 0 else 'fell'} {abs(moved):.0f}% while the desk was deciding")
            return "ran"
        try:
            back = self.broker.quote_sell(mint, q.raw)
        except ValueError:
            back = None
        except Exception as e:
            self._feed("SKIP", "CHIEF", f"${symbol}: re-quote failed ({type(e).__name__})")
            return "error"
        _, why = funnel.g3_depth(q, back, decimals, self.cfg.depth)  # the depth gate again, on the quote we would send
        if why:
            self._feed("SKIP", "CHIEF", f"${symbol}: at the re-quote, {'; '.join(why[:2])}")
            return "depth"
        info = {"symbol": symbol, "decimals": decimals, "station": station, "pool": pool, "decided_price": decided_price,
                "atr": atr, "vol5": vol5, "did": did, "source_wallet": source_wallet}
        with self.lock:  # last look, right before the money moves: the kill switch or a limit may have fired meanwhile
            blocked = self._buys_blocked()
            opens = self.journal.open_positions(self.mode)
            pend = [r for r in self.journal.pending(self.mode) if r["side"] == "buy"]
            if not blocked and len(opens) + len(pend) >= self.cfg.risk.max_open_positions:
                blocked = f"already {len(opens) + len(pend)} open positions"
            if not blocked and any(r["mint"] == mint for r in [*opens, *pend]):
                blocked = "already holding it"
            if not blocked:  # written down before it is sent, so a crash or an unknown outcome is settled, not lost
                pid = self.journal.add_pending(self.mode, "buy", mint, None, q.raw, q.usd, self.clock(), info)
                self._executing.add(pid)
        if blocked:
            self._feed("SKIP", "CHIEF", f"${symbol}: {blocked}")
            return "blocked"
        try:
            fill = self._execute(q)
            with self.lock:
                if fill is PENDING:
                    self._feed("WARN", "CHIEF", f"${symbol}: buy outcome unknown; settling it from the wallet's transactions", alert=True)
                    return "pending"
                if fill is None or fill.raw <= 0:
                    self.journal.drop_pending(pid)
                    return "fill-failed"
                self._open_real(info, mint, fill)  # booked first, crossed off second: a crash in between re-settles
                self.journal.drop_pending(pid)     # (and finds the buy already booked), it never forgets the coin
        finally:
            with self.lock:
                self._executing.discard(pid)
        return "buy"

    def _open_real(self, info: dict, mint: str, fill: Fill) -> None:
        dec = info["decimals"]
        fill_price = fill.usd / (fill.raw / 10 ** dec)
        plan, _ = make_plan(fill_price, info["atr"], info["vol5"], self.cfg.plan)
        if plan is None:  # the fill moved the levels so far the plan no longer passes: keep the decided one, re-based
            plan, _ = make_plan(info["decided_price"], info["atr"], info["vol5"], self.cfg.plan)
        if plan is None:  # the rules changed since the decision: a coin already bought still needs its exits
            plan, _ = make_plan(fill_price, info["atr"], info["vol5"], self.cfg.plan, strict=False)
        with self.lock:
            self._book_version += 1
            self.journal.open_position(mode=self.mode, book="real", mint=mint, symbol=info["symbol"], decimals=dec, station=info["station"],
                                       pool=info["pool"], opened_t=int(self.clock()), cost_usd=fill.usd, raw=fill.raw, entry_price=fill_price,
                                       plan=plan.to_dict(), decision_id=info["did"], buy_ref=fill.ref, source_wallet=info["source_wallet"])
            self._priced(mint, fill_price)
            self.journal.count(self.mode, self.clock(), "filled")
        self._feed("BUY", "CHIEF", f"${info['symbol']} · ${fill.usd:,.2f} at {fill_price:.6g} · stop {plan.stop:.6g} (-{plan.stop_pct:.0f}%) · "
                                   f"TP {', '.join(f'+{p:g}%' for p in self.cfg.plan.ladder_pct)}", alert=True)

    def _execute(self, q):
        """Send it. Returns the Fill, None when it certainly failed, or PENDING when nobody knows yet (live only):
        the pending row written before sending is then settled from the wallet's transactions (_settle_pending)."""
        try:
            fill = self.broker.execute(q)
            self._api_ok("swap")
            return fill
        except SwapOutcomeUnknown as e:
            log.warning("swap outcome unknown (%s); it will be settled from the chain", e)
            return PENDING
        except Exception as e:
            self._swap_failed(q.side, f"swap: {type(e).__name__}: {e}")
            return None

    def _swap_failed(self, side: str, what: str) -> None:
        """Failed buys count toward the kill switch. Failed sells don't: they are counted per position in _sell, so
        one coin nobody can sell doesn't fire the kill switch (and sell everything else) over and over."""
        if side == "buy":
            self._api_error(what, "swap")
        else:
            log.warning("%s", what)

    def _settle_pending(self) -> None:
        """Swaps written down but not known to have landed or failed: an unknown outcome, or a crash mid-swap.
        Settled from the wallet's transactions; when those stay unreadable, from the wallet balance after a while, so
        a position is never left without its stop because its last sell can't be settled."""
        now = self.clock()
        with self.lock:
            rows = [r for r in self.journal.pending(self.mode) if r["id"] not in self._executing
                    and not (r["position_id"] and (r["position_id"] in self._inflight))]
        for r in rows:
            info = json.loads(r["info"] or "{}")
            if self.mode != "live":  # paper and demo swaps are instant: a row left over means a crash between two lines
                with self.lock:
                    self.journal.drop_pending(r["id"])
                log.warning("dropped a pending %s of %s left by a crash (paper)", r["side"], r["mint"][:6])
                continue
            sym = info.get("symbol") or r["mint"][:6]
            try:
                fill = self.broker.settle(r["mint"], r["side"], r["t"], skip=self.journal.booked_refs(self.mode, r["mint"]))
                self._api_ok("settle")
            except Exception as e:
                self._api_error(f"settling a swap: {type(e).__name__}: {e}", "settle")  # only a settle that works resets it
                if now - r["t"] >= SETTLE_GIVE_UP_SECONDS:
                    self._settle_from_balance(r, info, sym)
                continue
            if fill is not None:
                with self.lock:
                    if r["side"] == "buy":
                        self._open_real(info, r["mint"], fill)
                    else:
                        p = self.journal.position(r["position_id"])
                        if p and p["closed_t"] is None:
                            self._finish(p, min(fill.raw, p["raw"] - p["sold_raw"]), fill.usd, info.get("kind") or "retry", fill.ref,
                                         note=f"{info.get('note', '')} (settled from the chain)".strip())
                    self.journal.drop_pending(r["id"])  # after booking: a crash in between re-settles to 'already booked'
                continue
            if now - r["t"] >= SETTLE_SECONDS:  # its blockhash has expired: it can no longer land
                with self.lock:
                    self.journal.drop_pending(r["id"])
                self._feed("WARN", "CHIEF", f"${sym}: the {r['side']} whose outcome was unknown did not land"
                                            + ("; the sell will be retried" if r["side"] == "sell" else ""))

    def _settle_from_balance(self, r, info: dict, sym: str) -> None:
        """The wallet's history stayed unreadable. A sell: stop waiting, and let the retry sell what the wallet holds.
        A buy: if the wallet holds the coin and the journal doesn't, book it at the quoted cost (an estimate)."""
        if r["side"] == "sell":
            with self.lock:
                self.journal.drop_pending(r["id"])
            self._feed("WARN", "CHIEF", f"${sym}: couldn't read the chain to settle a sell; selling what the wallet holds", alert=True)
            return
        try:
            have = self.broker.token_raw(r["mint"])
        except Exception as e:
            return self._api_error(f"token balance: {e}", "wallet")
        with self.lock:
            if have > 0 and not any(p["mint"] == r["mint"] for p in self.journal.open_positions(self.mode)):
                self._open_real(info, r["mint"], Fill("buy", r["mint"], have, r["usd"], "wallet-balance"))
                self._feed("WARN", "CHIEF", f"${sym}: booked from the wallet balance; its cost is the quote's estimate", alert=True)
            self.journal.drop_pending(r["id"])

    # ---------------------------------------------------------------- shadow (rules-only arm)
    def _open_shadow(self, mint, symbol, decimals, station, pool, ticket, entry, plan: ExitPlan, did, impact_pct: float = 0.0) -> None:
        if any(p["mint"] == mint for p in self.journal.open_positions(self.mode, "shadow")):
            return  # one rules-only position per coin at a time, or a coin looked at again would count twice
        slip = self.cfg.paper.slippage_bps / 1e4  # the same costs the paper broker charges the real arm
        raw = int(ticket * (1 - slip) / entry * 10 ** decimals) if entry else 0
        if raw <= 0:
            return
        self.journal.open_position(mode=self.mode, book="shadow", mint=mint, symbol=symbol, decimals=decimals, station=station,
                                   pool=pool, opened_t=int(self.clock()), cost_usd=ticket, raw=raw, entry_price=entry,
                                   plan=plan.to_dict(), decision_id=did, buy_ref="shadow",
                                   exit_cost=slip + max(impact_pct, 0.0) / 100)  # selling back costs about what buying did
        self._priced(mint, entry)

    # ================================================================ the manager (main thread)
    def step(self) -> None:
        now = self.clock()
        if not self.threaded:
            try:
                self.scan_once()
                self._api_ok("scan")
            except Exception as e:
                log.exception("scan error")
                self._api_error(f"scan: {type(e).__name__}: {e}", "scan")
        if self.watcher is not None and self.tracked:
            self._poll_track()
        if now - self.last["price"] >= PRICE_SECONDS:
            self.last["price"] = now
            self.manage(volume_sample=now - self.last["volume"] >= VOLUME_SECONDS)
        if now - self.last["mark"] >= MARK_SECONDS:
            self.last["mark"] = now
            self.mark()

    def busy(self) -> bool:
        """A swap is being sent or waiting to be: shutting down now could leave it unaccounted for."""
        with self.lock:
            return bool(self._inflight or self._queued or self._executing)

    def _poll_track(self) -> None:
        try:
            with self.lock:
                held = {p["source_wallet"] for p in self.journal.open_positions(self.mode) if p["source_wallet"]}
            self.watcher.watch(set(self.tracked) | held)
            swaps = self.watcher.poll()
            self._api_ok("watch")
        except Exception as e:
            self._api_error(f"watching wallets: {type(e).__name__}: {e}", "watch")
            return
        for sw in swaps:
            name = self.tracked.get(sw.wallet, sw.wallet[:6])
            if sw.side == "buy" and sw.wallet in self.tracked:
                if sw.t and self.clock() - sw.t > self.cfg.track.max_signal_age_seconds:
                    continue
                self._feed("SIG", "SCOUT", f"tracked wallet {name} bought {sw.mint[:6]}… for ${sw.usd:,.0f}: checking it")
                self.queue.put({"mint": sw.mint, "wallet": sw.wallet})
            elif sw.side == "sell" and self.cfg.track.exit_when_wallet_sells and sw.sold_fraction >= 0.5:
                with self.lock:
                    mine = [p for p in self.journal.open_positions(self.mode) if p["mint"] == sw.mint and p["source_wallet"] == sw.wallet]
                for p in mine:
                    self._feed("SIG", "SCOUT", f"tracked wallet {name} sold {sw.sold_fraction:.0%} of ${p['symbol']}")
                    self._dispatch_sell(p, 1.0, "exit", f"the tracked wallet sold {sw.sold_fraction:.0%}")

    # ---------------------------------------------------------------- prices
    def _priced(self, mint: str, price: float) -> None:
        self.prices_now[mint] = price
        self.price_t[mint] = self.clock()
        self.unpriced.pop(mint, None)
        self._unpriced_n.pop(mint, None)

    def _dead(self, mint: str) -> bool:
        """Every source answered, round after round for UNPRICED_SECONDS, that nobody prices or buys this coin."""
        since = self.unpriced.get(mint)
        return (since is not None and self.clock() - since >= UNPRICED_SECONDS
                and self._unpriced_n.get(mint, 0) >= UNPRICED_SECONDS // PRICE_SECONDS)

    def _value_price(self, mint: str, fallback: float) -> float:
        """The price to value a holding at: the last one, unless the coin is dead (then 0)."""
        if self._dead(mint):
            return 0.0
        return self.prices_now.get(mint) or fallback

    def _refresh_prices(self, real: list, shadow: list, volume_sample: bool) -> bool:
        """Jupiter first; any coin it left out from DexScreener; anything still unpriced from a sell quote for what is
        held. A coin counts toward 'unpriced' only on rounds where the sources answered without it: a failed request
        says nothing about the coin, and is counted as an API error instead (so a long failure trips the kill switch
        rather than leaving an old price in place forever). Returns whether this round's volume sample was taken."""
        mints = list(dict.fromkeys(p["mint"] for p in real + shadow))
        mine = {p["mint"] for p in real}  # only these can fire the kill switch: a fake-money coin never sells real ones
        now = self.clock()
        try:
            got: dict[str, float] = {}
            for i in range(0, len(mints), 50):
                got.update(self.prices.get(mints[i:i + 50]))
            self._api_ok("prices")
        except Exception as e:
            self._api_error(f"prices: {type(e).__name__}", "prices")
            return False
        for m, v in got.items():
            self._priced(m, v)
        missing = [m for m in mints if m not in got]
        pairs, dex_failed = None, False
        if volume_sample or missing:
            try:
                pairs = self.mdex.tokens(mints if volume_sample else missing)
            except Exception as e:
                dex_failed = True
                log.info("DexScreener (manager) failed: %s", type(e).__name__)
        if volume_sample:
            self.last["volume"] = now
            if pairs is None:
                volume_sample = False
            else:
                for m, pair in pairs.items():
                    v = (pair.get("volume") or {}).get("m5")
                    if v is not None:
                        self.vol_now[m] = float(v)
        if not missing:
            self._api_ok("pricing")
            return volume_sample
        if dex_failed:
            if mine & set(missing):
                self._api_error("DexScreener prices for coins Jupiter can't price", "pricing")
            return volume_sample
        held: dict[str, tuple[int, int]] = {}
        for p in real + shadow:  # what is held of each coin (the real book first), to ask for a sell quote
            if p["mint"] not in held and p["raw"] - p["sold_raw"] > 0:
                held[p["mint"]] = (p["raw"] - p["sold_raw"], p["decimals"])
        failed = False
        for m in missing:
            px = _num((pairs.get(m) or {}).get("priceUsd"))
            if px and px > 0:
                self._priced(m, px)
                continue
            if m in held:
                raw, dec = held[m]
                try:
                    q = self.broker.quote_sell(m, raw)
                    if q.usd > 0 and q.raw:
                        self._priced(m, q.usd / (q.raw / 10 ** dec))
                        continue
                except Exception as e:
                    if not _no_route(e):
                        failed = failed or m in mine
                        log.info("sell quote for pricing failed: %s", type(e).__name__)
                        continue
            self._unpriced_n[m] = self._unpriced_n.get(m, 0) + 1  # the sources answered: nobody prices it
            if m not in self.unpriced:
                self.unpriced[m] = now
                sym = next((p["symbol"] for p in real + shadow if p["mint"] == m), m[:6])
                self._feed("WARN", "CHIEF", f"no price for ${sym} from Jupiter, DexScreener or a sell quote")
        if failed:
            self._api_error("sell quotes for coins nobody else prices", "pricing")
        else:
            self._api_ok("pricing")
        return volume_sample

    def manage(self, volume_sample: bool = False) -> None:
        with self.lock:
            real = self.journal.open_positions(self.mode, "real")
            shadow = self.journal.open_positions(self.mode, "shadow")
        if not real and not shadow:
            return
        volume_sample = self._refresh_prices(real, shadow, volume_sample)
        now = self.clock()
        sells: list[tuple] = []
        with self.lock:
            settling = {r["position_id"] for r in self.journal.pending(self.mode) if r["position_id"]}
            for p in self.journal.open_positions(self.mode, "real") + self.journal.open_positions(self.mode, "shadow"):
                real_book = p["book"] == "real"
                if real_book and (p["id"] in self._inflight or p["id"] in self._queued or p["id"] in settling):
                    continue
                if real_book and self.kill.tripped:
                    sells.append((p, 1.0, "kill", "kill switch"))
                    continue
                if real_book and p["exit_pending"]:
                    if now >= self._retry_at.get(p["id"], 0):
                        sells.append((p, float(p["exit_pending"]), p["pending_kind"] or "retry", "retrying a failed sell"))
                    continue
                if self._dead(p["mint"]):
                    note = f"no price from any source for {(now - self.unpriced[p['mint']]) / 60:.0f} min: treating it as dead"
                    if real_book:
                        sells.append((p, 1.0, "stop", note))
                    else:
                        self._finish(p, p["raw"] - p["sold_raw"], 0.0, "stop", "shadow", note=note)
                    continue
                price = self.prices_now.get(p["mint"])
                if not price or p["mint"] in self.unpriced:
                    continue
                plan = ExitPlan.from_dict(json.loads(p["plan"]))
                left = (p["raw"] - p["sold_raw"]) / p["raw"] if p["raw"] else 0.0
                act = plan_step(plan, price, self.vol_now.get(p["mint"]), now - p["opened_t"], left, volume_sample)
                self.journal.update_position(p["id"], plan=plan.to_dict())
                if not act:
                    continue
                if real_book:
                    sells.append((self.journal.position(p["id"]), act.fraction, act.kind, act.note))
                else:
                    self._shadow_sell(self.journal.position(p["id"]), act.fraction, act.kind, price)
        for p, fraction, kind, note in sells:
            self._dispatch_sell(p, fraction, kind, note)

    def _dispatch_sell(self, p, fraction: float, kind: str, note: str) -> None:
        """Live and paper: each sell on a worker, so one slow swap never holds up the other stops or the limits."""
        if self._pool is None:
            self._sell(p, fraction, kind, note)
            return
        with self.lock:
            if p["id"] in self._queued or p["id"] in self._inflight:
                return
            self._queued.add(p["id"])
        self._pool.submit(self._sell_job, p, fraction, kind, note)

    def _sell_job(self, p, fraction: float, kind: str, note: str) -> None:
        try:
            self._sell(p, fraction, kind, note)
        except Exception:
            log.exception("sell of %s failed", p["symbol"])
        finally:
            with self.lock:
                self._queued.discard(p["id"])

    def _shadow_sell(self, p, fraction: float, kind: str, price: float) -> None:
        left = p["raw"] - p["sold_raw"]
        raw = left if fraction >= 0.999 else int(left * fraction)
        cost = p["exit_cost"] if p["exit_cost"] else self.cfg.plan.round_trip_cost_pct / 2 / 100
        usd = raw / 10 ** p["decimals"] * price * (1 - cost)
        self._finish(p, raw, usd, kind, "shadow")

    def _sell(self, p, fraction: float, kind: str, note: str) -> bool:
        """Sell `fraction` of what is left. Live, the wallet balance caps the amount when it can be read; when it can't,
        the sell still goes (Jupiter refuses an order the wallet can't cover, and the next round settles it)."""
        pid = p["id"]
        with self.lock:
            p = self.journal.position(pid)
            if p["closed_t"] is not None or pid in self._inflight:
                return p["closed_t"] is not None
            if any(r["position_id"] == pid for r in self.journal.pending(self.mode)):
                return False  # a sell of it is still being settled
            left = p["raw"] - p["sold_raw"]
            if left <= 0:
                return True
            raw = left if fraction >= 0.999 else int(left * fraction)
            price = self._value_price(p["mint"], p["entry_price"])
            if (left - raw) / 10 ** p["decimals"] * price < DUST_USD:
                raw = left
            self._inflight.add(pid)
        n = 0
        try:
            fill, why, no_route, have = None, "", False, None
            if self.mode == "live":
                try:
                    have = self.broker.token_raw(p["mint"])
                    self._api_ok("wallet")
                except Exception as e:
                    self._api_error(f"token balance: {e}", "wallet")
                if have is not None:
                    self._empty_reads[pid] = self._empty_reads.get(pid, 0) + 1 if have <= 0 else 0
                    raw = min(raw, have)
                    if raw <= 0 and self._empty_reads[pid] >= SHORT_READS:
                        with self.lock:
                            self._empty_reads.pop(pid, None)
                            p = self.journal.position(pid)
                            return self._finish(p, p["raw"] - p["sold_raw"], 0.0, kind, "wallet-empty", note="wallet already empty")
                    if raw <= 0:
                        why = "wallet shows none of it yet; reading again"
            if raw > 0:
                try:
                    q = self.broker.quote_sell(p["mint"], raw)
                except Exception as e:
                    why, no_route = f"{type(e).__name__}: {e}", _no_route(e)
                else:
                    with self.lock:
                        spid = self.journal.add_pending(self.mode, "sell", p["mint"], pid, raw, q.usd, self.clock(),
                                                        {"kind": kind, "note": note, "symbol": p["symbol"]})
                    fill = self._execute(q)
                    if fill is PENDING:
                        with self.lock:
                            self.journal.update_position(pid, exit_pending=max(float(p["exit_pending"] or 0), fraction),
                                                         pending_kind=p["pending_kind"] or kind)
                        self._feed("WARN", "CHIEF", f"${p['symbol']}: sell outcome unknown; settling it from the wallet's transactions")
                        return False
                    with self.lock:
                        self.journal.drop_pending(spid)
                    if fill is None:
                        why = "the swap failed"
            with self.lock:
                p = self.journal.position(pid)
                if fill is not None:
                    for d in (self._sell_failures, self._no_route_n, self._retry_at):
                        d.pop(pid, None)
                    return self._finish(p, fill.raw, fill.usd, kind, fill.ref, note=note)
                n = self._sell_failures[pid] = self._sell_failures.get(pid, 0) + 1
                if no_route and (self.mode != "live" or (have or 0) > 0):  # only proof nobody will buy it counts
                    self._no_route_n[pid] = self._no_route_n.get(pid, 0) + 1
                worth = self._value_price(p["mint"], p["entry_price"]) * (p["raw"] - p["sold_raw"]) / 10 ** p["decimals"]
                if self._no_route_n.get(pid, 0) >= WRITE_OFF_AFTER and worth < DUST_USD * 10:
                    for d in (self._sell_failures, self._no_route_n, self._retry_at):
                        d.pop(pid, None)
                    return self._finish(p, p["raw"] - p["sold_raw"], 0.0, kind, "none", note="no route to sell it: written off as worthless")
                self.journal.update_position(pid, exit_pending=max(float(p["exit_pending"] or 0), fraction), pending_kind=p["pending_kind"] or kind)
                if n >= WRITE_OFF_AFTER:
                    self._retry_at[pid] = self.clock() + SLOW_RETRY_SECONDS
            if why and (n == 1 or n % WRITE_OFF_AFTER == 0):  # not on every 10 s retry
                self._feed("WARN", "CHIEF", f"can't sell ${p['symbol']} yet: {why}"[:300])
            wait = SLOW_RETRY_SECONDS if n >= WRITE_OFF_AFTER else PRICE_SECONDS
            self._alert(f"SELL of ${p['symbol']} failed ({note}); retrying every {wait}s.")
            return False
        finally:
            with self.lock:
                self._inflight.discard(pid)

    def _finish(self, p, sold_raw: int, usd: float, kind: str, ref: str, note: str = "") -> bool:
        now = self.clock()
        self._book_version += 1
        cost_share = p["cost_usd"] * sold_raw / p["raw"] if p["raw"] else 0.0
        self.journal.record_exit(mode=self.mode, book=p["book"], position_id=p["id"], t=int(now), kind=kind, raw=sold_raw,
                                 usd=usd, cost_share=cost_share, ref=ref)
        sold_total, sold_usd = p["sold_raw"] + sold_raw, p["sold_usd"] + usd
        closed = sold_total >= p["raw"] or sold_raw == 0
        upd = {"sold_raw": sold_total, "sold_usd": sold_usd, "exit_pending": 0, "pending_kind": None, "last_ref": ref}
        if not closed:
            self.journal.update_position(p["id"], **upd)
            if p["book"] == "real":
                self._feed("SELL", "CHIEF", f"${p['symbol']} · {kind} · sold {sold_raw / p['raw']:.0%} ({note})", usd - cost_share)
            return True
        pnl = sold_usd - p["cost_usd"]
        self.journal.update_position(p["id"], **upd, closed_t=int(now), pnl=pnl, exit_kind=kind)
        self._short_reads.pop(p["id"], None)
        if p["book"] == "shadow":
            return True
        self._feed("EXIT", "CHIEF", f"${p['symbol']} · {kind} · {note}", pnl, alert=True)
        streak = 0 if pnl > 0 else int(self.journal.get(f"loss_streak:{self.mode}", 0)) + 1
        self.journal.put(f"loss_streak:{self.mode}", streak)
        if streak >= self.cfg.risk.pause_after_losses:
            self.journal.put(f"paused_until:{self.mode}", now + self.cfg.risk.pause_hours * 3600)
            self.journal.put(f"loss_streak:{self.mode}", 0)
            self._feed("STOP", "CHIEF", f"{streak} losses in a row: no new buys for {self.cfg.risk.pause_hours:g}h", alert=True)
        return True

    # ---------------------------------------------------------------- equity and hard limits
    def equity(self, cash: float | None = None) -> float:
        """USDC + open coins at their value + buys sent but not yet booked (their USDC may have left while their coins
        aren't in the journal yet: without this a buy in flight reads as a loss and can fire a limit)."""
        low, pending = self._equity_parts(self.broker.usdc() if cash is None else cash)
        return low + pending

    def _equity_parts(self, cash: float) -> tuple[float, float]:
        """(USDC + booked coins, USD of buys in flight). The second may or may not have left the wallet yet."""
        with self.lock:
            value = sum((p["raw"] - p["sold_raw"]) / 10 ** p["decimals"] * self._value_price(p["mint"], p["entry_price"])
                        for p in self.journal.open_positions(self.mode))
            pending = sum(r["usd"] for r in self.journal.pending(self.mode) if r["side"] == "buy")
        return cash + value, pending

    def mark(self) -> None:
        """Equity, its high, the day's start, and the hard limits. The wallet read happens outside the lock, so a sell
        booked by a worker during that read would make the numbers disagree: such a mark is skipped. Limits use the
        equity with buys in flight counted (no false loss); the high and the day's start use it without them (no
        inflated baseline); and a limit fires only when two marks in a row see it."""
        now, r = self.clock(), self.cfg.risk
        version = self._book_version
        try:
            cash = self.broker.usdc()
            self._api_ok("wallet")
        except Exception as e:
            return self._api_error(f"wallet: {type(e).__name__}: {e}", "wallet")
        trip_reason = None
        with self.lock:
            if self._book_version != version:
                return
            low, pending = self._equity_parts(cash)
            eq = low + pending
            self.equity_now = eq
            self.journal.put(f"equity:{self.mode}", eq)
            self.journal.equity(self.mode, now, eq)
            if self.journal.get(f"start:{self.mode}") is None:
                self.journal.put(f"start:{self.mode}", low)
                self.journal.put(f"start_t:{self.mode}", now)
            peak = max(float(self.journal.get(f"peak:{self.mode}") or low), low)
            self.journal.put(f"peak:{self.mode}", peak)
            day = day_of(now)
            if self.journal.get(f"day:{self.mode}") != day or self.journal.get(f"day_start:{self.mode}") is None:
                self.journal.put(f"day:{self.mode}", day)
                self.journal.put(f"day_start:{self.mode}", low)
            day_start = float(self.journal.get(f"day_start:{self.mode}") or eq)
            reset_at = self.journal.get(f"reset_at:{self.mode}")
            if reset_at != self._reset_seen:  # ./desk.sh reset while running: forget the streaks it cleared
                self._reset_seen = reset_at
                self.errors = {}
                self._save_errors()
            if not self.kill.tripped:
                dd = peak > 0 and (peak - eq) / peak * 100 >= r.max_drawdown_pct
                daily = day_start > 0 and (day_start - eq) / day_start * 100 >= r.daily_loss_limit_pct
                self._breaches = {k: self._breaches.get(k, 0) + 1 for k, hit in (("drawdown", dd), ("daily", daily)) if hit}
                if self._breaches.get("drawdown", 0) >= 2:
                    trip_reason = f"equity fell {(peak - eq) / peak:.0%} from its high (limit {r.max_drawdown_pct:g}%)"
                elif self._breaches.get("daily", 0) >= 2 and self.journal.get(f"daily_stop:{self.mode}") != day:
                    self.journal.put(f"daily_stop:{self.mode}", day)
                    self._feed("STOP", "CHIEF", f"down {(day_start - eq) / day_start:.0%} today: no new buys until 00:00 UTC", alert=True)
        if trip_reason:
            self.trip(trip_reason)
        self._settle_pending()
        if self.mode == "live":
            self._reconcile()

    def _reconcile(self) -> None:
        """Live: does the wallet still hold what the journal says? Tokens can leave outside the bot (a manual sell in
        Phantom). Only a shortfall seen on several reads in a row counts, and never on a buy that just landed (RPC
        balances lag) or a position with a sell in flight or being settled, and the missing tokens are booked as an
        exit for nothing, so the loss shows in the P&L."""
        try:
            self.sol_now = self.broker.sol()
            held = self.broker.holdings()
        except Exception as e:
            return self._api_error(f"wallet: {e}", "wallet")
        now = self.clock()
        with self.lock:
            settling = {r["position_id"] for r in self.journal.pending(self.mode) if r["position_id"]}
            for p in self.journal.open_positions(self.mode):
                have, want = held.get(p["mint"], 0), p["raw"] - p["sold_raw"]
                if (p["id"] in self._inflight or p["id"] in self._queued or p["id"] in settling
                        or now - p["opened_t"] < RECONCILE_GRACE_SECONDS or have >= want * 0.95):
                    self._short_reads.pop(p["id"], None)
                    continue
                n = self._short_reads[p["id"]] = self._short_reads.get(p["id"], 0) + 1
                if n < SHORT_READS:
                    continue
                self._short_reads.pop(p["id"], None)
                self._feed("WARN", "CHIEF", f"wallet holds less ${p['symbol']} than expected: {have} instead of {want}")
                self._finish(p, want - have, 0.0, "manual", "none", note="tokens left the wallet outside the bot")

    def trip(self, reason: str, sell: bool = True) -> None:
        """Kill switch on. With sell=False (the dashboard button, which holds the lock) the manager does the selling,
        within PRICE_SECONDS, instead of the caller's thread."""
        self.kill.trip(reason)
        self._feed("KILL", "CHIEF", f"{reason} · selling everything and stopping", alert=True)
        if not sell:
            self.last["price"] = -1e18  # the manager's next step sells
            return
        with self.lock:
            opens = self.journal.open_positions(self.mode)
        for p in opens:
            self._dispatch_sell(p, 1.0, "kill", "kill switch")

    # ================================================================ dashboard
    def dashboard_state(self) -> dict:
        now = self.clock()
        j, mode = self.journal, self.mode
        day = day_of(now)
        start = float(j.get(f"start:{mode}") or self.equity_now or 0)
        start_t = float(j.get(f"start_t:{mode}") or now)
        curve = [(r["t"], r["v"]) for r in j.equity_curve(mode, points=600)]
        real_closed, shadow_closed = j.closed_positions(mode, "real"), j.closed_positions(mode, "shadow")
        by_station: dict[str, dict] = {}
        for p in real_closed:
            s = by_station.setdefault(p["station"], {"n": 0, "pnl": 0.0, "wins": 0})
            s["n"] += 1
            s["pnl"] += p["pnl"]
            s["wins"] += int(p["pnl"] > 0)
        positions = []
        for p in j.open_positions(mode):
            price = self._value_price(p["mint"], p["entry_price"])
            plan = json.loads(p["plan"])
            left = (p["raw"] - p["sold_raw"]) / p["raw"] if p["raw"] else 0
            positions.append({"symbol": p["symbol"], "mint": p["mint"], "station": p["station"], "entry": p["entry_price"], "price": price,
                              "pnlPct": round((price / p["entry_price"] - 1) * 100, 1) if p["entry_price"] else 0,
                              "cost": round(p["cost_usd"] * left, 2), "value": round((p["raw"] - p["sold_raw"]) / 10 ** p["decimals"] * price, 2),
                              "stop": plan["stop"], "rungs": plan["rungs"], "rungsDone": plan["rungs_done"], "book": plan["book"],
                              "ageMin": int((now - p["opened_t"]) / 60), "left": round(left, 3), "unpriced": p["mint"] in self.unpriced})
        decisions = [{"t": d["t"], "symbol": d["symbol"], "station": d["station"], "action": d["action"], "verdict": d["verdict"],
                      "conviction": d["conviction"], "note": d["note"]} for d in j.decisions(mode, 12)]
        calib = {}
        for d in j.conviction_outcomes(mode):
            b = "high" if d["c"] >= 0.7 else "mid" if d["c"] >= 0.4 else "low"
            c = calib.setdefault(b, {"n": 0, "wins": 0})
            c["n"] += 1
            c["wins"] += int(d["pnl"] > 0)
        return {
            "mode": mode, "now": now, "day": int((now - start_t) // 86400) + 1, "killed": self.kill.tripped, "killReason": self.kill.reason,
            "blocked": self._buys_blocked(), "scanState": self.scan_state,
            "balance": round(self.equity_now, 2), "start": round(start, 2), "pnl": round(self.equity_now - start, 2),
            "pnlPct": round((self.equity_now / start - 1) * 100, 1) if start else 0.0, "equity": curve,
            "funnel": {g: j.funnel(mode, day).get(g, 0) for g in funnel.GATES},
            "kills": [{"gate": r["gate"], "reason": r["reason"], "n": r["n"]} for r in j.top_kills(mode, day)],
            "latest": self.latest, "positions": positions, "decisions": decisions,
            "desk": [{"t": e["t"], "seat": e["seat"], "kind": e["kind"], "text": e["text"]} for e in j.events(mode, 30, SEATS[1:4])],
            "feed": [{"t": e["t"], "seat": e["seat"], "kind": e["kind"], "text": e["text"], "pnl": e["pnl"]} for e in j.events(mode, 80)],
            "stations": by_station,
            "exits": [{"kind": r["kind"], "n": r["n"], "pnl": round(r["pnl"] or 0, 2)} for r in j.exits_by_kind(mode)],
            "real": trade_stats(real_closed), "shadow": trade_stats(shadow_closed), "calibration": calib,
            "ai": {"spent": round(getattr(self.desk, "spent_today", lambda: 0.0)(), 4), "budget": self.cfg.desk.daily_budget_usd,
                   "model": self.cfg.desk.model, "enabled": self.cfg.desk.enabled},
            "limits": {"position_pct": self.cfg.risk.position_pct, "maxPosition": self.cfg.risk.max_position_usd,
                       "maxOpen": self.cfg.risk.max_open_positions, "maxDrawdown": self.cfg.risk.max_drawdown_pct,
                       "dailyLoss": self.cfg.risk.daily_loss_limit_pct},
            "health": {k: v for k, v in self.errors.items() if v},
        }
