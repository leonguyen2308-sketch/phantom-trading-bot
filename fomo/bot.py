"""The copy-trading loop.

Every second: new trades by the trader we copy (and by anyone whose copy we still hold).
  - they buy  -> checks, size (0.25x theirs, capped), buy the same token for USDC
  - they sell -> sell the same share of our copy
Every 15 seconds: prices, stop-loss / take-profit / max-hold, equity, the hard limits.
When a copy closes, its P&L goes to the trader's record; N losses in a row fires them and the next
trader on the weekly top takes over.

The Grok bot (config/grok.yaml) adds: a daily copy window (22:00-08:00 in the video), two traders copied at once,
the video's notifications, and xAI's Grok, which orders the bench and reads X about each coin we copy. Grok runs on
a worker thread: a copy never waits for it, and it can only change who is copied next or make us sell sooner.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from fomo.brokers import Fill, Quote
from fomo.config import FomoConfig, window_bounds
from fomo.journal import Journal
from fomo.leaderboard import Trader
from fomo.rules import Rotation
from fomo.safety import quote_vetoes, token_vetoes
from fomo.swaps import USDC
from phantom.execution.jupiter import SwapOutcomeUnknown
from phantom.risk import KillSwitch

log = logging.getLogger(__name__)

MARK_SECONDS = 15
DUST_USD = 0.50
WRITE_OFF_AFTER = 20  # failed sells in a row (about 5 minutes) before a token with no route out counts as worthless
ALERT_REPEAT_SECONDS = 300
CROWD_ALERTS = {"KILL", "STOP", "WARN"}  # copying hundreds of traders: Telegram only hears about what needs you
TOKEN_VETO_SECONDS = 300  # a coin refused for what it is (authorities, pool size) is not looked up again for this long
DEX_CACHE_SECONDS = 15  # one coin bought by several traders is read from DexScreener once
GROK_NOTE_SECONDS = 600  # "Grok unavailable" is said at most this often


class CopyBot:
    def __init__(self, *, mode: str, cfg: FomoConfig, broker, watcher, prices, dex, mint_info: Callable,
                 lineup: Callable[[], list[Trader]], journal: Journal, kill: KillSwitch, alerts=None, clock=time.time, grok=None,
                 pool=None):
        self.mode, self.cfg, self.broker, self.watcher, self.prices, self.dex = mode, cfg, broker, watcher, prices, dex
        self.mint_info, self.lineup_source, self.journal, self.kill, self.alerts, self.clock = mint_info, lineup, journal, kill, alerts, clock
        self.rot = Rotation(cfg.copy.fire_after_losses, cfg.copy.fire_cooldown_days * 86400, journal.get(f"rotation:{mode}"),
                            follow_all=cfg.copy.follow_all, follow_count=cfg.copy.follow_count)
        self.errors: dict[str, int] = {"general": int(journal.get(f"api_errors:{mode}", 0))}  # failing 15-second windows in a row, per source
        self._error_window: dict[str, float] = {}
        self.equity_now = float(journal.get(f"equity:{mode}", 0.0))
        self.marks: dict[str, float] = {}
        self._mints: dict[str, object] = {}
        self._last_mark = -1e18
        self._alerted: dict[str, float] = {}
        self._sell_failures: dict[int, int] = {}
        self.sol_now: float | None = None  # live: SOL for fees, read at every 15-second check
        self._lineup_retry_at = 0.0
        self._last_skip = ("", 0.0)
        self._by_wallet: dict[str, Trader] = {}
        self._by_wallet_src = None
        self._signals: deque[float] = deque()  # when each followed trader's trade arrived (last hour)
        self._copies: deque[float] = deque()  # when each copy was made (last hour)
        self._token_veto: dict[str, tuple[float, str]] = {}
        self._dex_cache: dict[str, tuple[float, object]] = {}
        self._exit_lag: dict[int, float] = {}  # copy id -> seconds between the trader's sell and ours
        self.grok = grok if grok is not None and cfg.grok.enabled else None
        self._jobs: list[tuple[str, object, dict, float]] = []  # (kind, future, context, started)
        self._pool = pool or (ThreadPoolExecutor(max_workers=2, thread_name_prefix="grok") if self.grok else None)
        self._pending_lineup: list[Trader] | None = None
        self._grok_note_at: dict[str, float] = {}
        if self.grok:
            day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
            self.grok.restore(day, float(journal.get(f"grok_spend:{mode}:{day}", 0.0) or 0.0))

    # ------------------------------------------------------------ helpers
    def _save_rotation(self) -> None:
        self.journal.put(f"rotation:{self.mode}", self.rot.s)

    def _feed(self, kind: str, text: str, pnl: float | None = None, alert: bool = False) -> None:
        self.journal.event(self.mode, self.clock(), kind, text, pnl)
        log.info("[%s] %s%s", kind, text, f" ({pnl:+.2f})" if pnl is not None else "")
        if alert and (not self.cfg.copy.follow_all or kind in CROWD_ALERTS):
            self._alert(f"[{kind}] {text}" + (f"  {pnl:+.2f} USD" if pnl is not None else ""))

    def _alert(self, text: str) -> None:
        key = text[:60]
        if time.monotonic() - self._alerted.get(key, -1e9) < ALERT_REPEAT_SECONDS:
            return
        self._alerted[key] = time.monotonic()
        if self.alerts:
            try:
                self.alerts.send(f"[{self.mode.upper()}] {text}")
            except Exception as e:  # alerts never break trading
                log.warning("alert failed: %s", type(e).__name__)

    @property
    def api_errors(self) -> int:
        return max(self.errors.values(), default=0)

    def _api_error(self, what: str, source: str = "general") -> None:
        """Counted once per 15 seconds per source (watching, wallet, swaps): the loop runs every second, so a
        10-second hiccup must not fire the kill switch. Ten in a row is about 2.5 minutes of failure. Each source
        has its own streak, so a working wallet can't hide a broken trade feed."""
        now = self.clock()
        if now - self._error_window.get(source, -1e18) < MARK_SECONDS:
            log.warning("API error (%s, same window): %s", source, what)
            return
        self._error_window[source] = now
        self.errors[source] = self.errors.get(source, 0) + 1
        self.journal.put(f"api_errors:{self.mode}", self.api_errors)
        log.warning("API error %d (%s): %s", self.errors[source], source, what)
        if self.errors[source] >= self.cfg.limits.max_consecutive_api_errors and not self.kill.tripped:
            self.trip(f"{self.errors[source]} API errors in a row ({source}; last: {what})")

    def _api_ok(self, source: str = "general") -> None:
        if self.errors.get(source):
            self.errors[source] = 0
            self.errors["general"] = 0
            self.journal.put(f"api_errors:{self.mode}", self.api_errors)

    @staticmethod
    def remaining(c) -> int:
        return int(c["raw"]) - int(c["sold_raw"])

    def _mint(self, mint: str):
        if mint not in self._mints:
            self._mints[mint] = self.mint_info(mint)
        return self._mints[mint]

    # ------------------------------------------------------------ the weekly top
    def refresh_lineup(self, force: bool = False) -> None:
        if self._pending_lineup is not None:
            return  # Grok is still putting the last one in order
        now = self.clock()
        due = now - float(self.journal.get(f"lineup_at:{self.mode}", 0)) >= self.cfg.copy.refresh_hours * 3600
        if not (force or due or not self.rot.s["lineup"]) or (not force and now < self._lineup_retry_at):
            return
        self._lineup_retry_at = now + 60
        try:
            traders = self.lineup_source()
        except Exception as e:  # not an API-error streak: a bad traders.yaml must not fire the kill switch
            log.warning("Loading the weekly top failed: %s", e)
            self._alert(f"Loading the weekly top failed: {e}")
            return
        if not traders:
            self._alert("The top list is empty: set SOLANATRACKER_API_KEY or FOMOAPI_KEY in .env, or put wallets in config/wallets.txt.")
            return
        traders = traders[: self.cfg.copy.lineup_size]
        self.journal.put(f"lineup_at:{self.mode}", now)
        if self._wants_picks():
            self._pending_lineup = traders  # set once Grok has put it in order (or failed), a few seconds from now
            self._ask_picks(traders)
            return
        self._set_lineup(traders)

    def _set_lineup(self, traders: list[Trader]) -> None:
        before = self.rot.s["following"]
        for e in self.rot.set_lineup(traders, self.clock()):
            self._feed("SCAN" if "loaded" in e else "PICK", e, alert=self.rot.s["following"] != before)
        self._save_rotation()

    # ------------------------------------------------------------ Grok (on a worker thread; results applied here)
    def _wants_picks(self) -> bool:
        return bool(self.grok and self.cfg.grok.pick_traders and not self.cfg.copy.follow_all and self.grok.available()[0])

    def _submit(self, kind: str, fn, *args, context: dict | None = None) -> None:
        self._jobs.append((kind, self._pool.submit(fn, *args), context or {}, self.clock()))

    def _ask_picks(self, traders: list[Trader]) -> None:
        now = self.clock()
        cands = []
        for t in [t for t in traders if not self.rot.is_fired(t.wallet, now)][: self.cfg.grok.pick_from]:
            st = self.rot.stats(t.wallet)
            cands.append({"wallet": t.wallet, "rank": t.rank, "pnl_7d": t.pnl_7d, "win_rate": t.win_rate, "copies": st["trades"],
                          "wins": st["wins"], "pnl_with_us": st["pnl"], "streak": st["streak"], "last": st["last"]})
        if cands:
            self._submit("picks", self.grok.rank_traders, cands, self.cfg.copy.follow_count)
        elif self._pending_lineup is not None:
            self._set_lineup(self._pending_lineup)
            self._pending_lineup = None

    def _grok_note(self, kind: str, text: str) -> None:
        if self.clock() - self._grok_note_at.get(kind, -1e18) >= GROK_NOTE_SECONDS:
            self._grok_note_at[kind] = self.clock()
            self._feed("GROK", text)

    def _run_jobs(self) -> None:
        from fomo.grok import GrokUnavailable
        now, keep = self.clock(), []
        for kind, fut, ctx, started in self._jobs:
            if not fut.done():
                if kind == "picks" and self._pending_lineup is not None and now - started > self.cfg.grok.timeout_seconds + 30:
                    self._grok_note("picks", "Grok took too long to order the traders: copying in leaderboard order")
                    self._set_lineup(self._pending_lineup)
                    self._pending_lineup = None
                keep.append((kind, fut, ctx, started))
                continue
            try:
                result = fut.result()
            except GrokUnavailable as e:
                result = None
                self._grok_note(kind, f"Grok unavailable ({e}): " + ("copying in leaderboard order" if kind == "picks" else "no X check"))
            except Exception as e:  # never let a Grok problem stop copying
                result = None
                log.warning("Grok %s failed: %s: %s", kind, type(e).__name__, e)
                self._grok_note(kind, f"Grok {kind} failed ({type(e).__name__}): carrying on without it")
            if kind == "picks":
                self._apply_picks(result)
            elif kind == "xcheck" and result:
                self._apply_xcheck(ctx, result)
        self._jobs = keep
        if self.grok:
            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            spent = self.grok.spent_today()
            if spent:
                self.journal.put(f"grok_spend:{self.mode}:{day}", spent)

    def _apply_picks(self, result) -> None:
        now = self.clock()
        if result:
            order, reasons = result
            self.rot.s["order"] = list(order)
            top = [w for w in order if not self.rot.is_fired(w, now)][: max(self.cfg.copy.follow_count, 3)]
            known = {t.wallet: t for t in (self._pending_lineup or [])}
            names = []
            for w in top:
                t = self._trader(w) or known.get(w)
                names.append(f"@{t.handle if t else w[:6]} (#{t.rank if t else '?'}): {reasons.get(w, '')}".rstrip(": "))
            self._feed("GROK", "Grok ordered the weekly top · " + " · ".join(names)[:600])
        if self._pending_lineup is not None:
            self._set_lineup(self._pending_lineup)
            self._pending_lineup = None
        elif result:
            for e in self.rot.set_order(self.rot.s["order"], now):
                self._feed("PICK", e)
            self._save_rotation()

    def _apply_xcheck(self, ctx: dict, result: dict) -> None:
        verdict, why = result.get("verdict"), "; ".join(result.get("reasons") or []) or "no details"
        held = next((c for c in self.journal.open_copies(self.mode) if c["mint"] == ctx["mint"]), None)
        if verdict == "rug" and held:
            self._feed("GROK", f"Grok read X: ${ctx['symbol']} looks like a rug ({why}) · selling now", alert=True)
            self.sell(held, 1.0, "Grok: X says rug")
        elif verdict == "warn":
            self._feed("GROK", f"Grok read X: a warning about ${ctx['symbol']} ({why}) · keeping it, the stop loss still applies")
        elif verdict == "ok":
            log.info("Grok X check: $%s ok", ctx["symbol"])

    # ------------------------------------------------------------ the copy window
    def in_window(self, now: float | None = None) -> bool:
        w = self.cfg.copy.window
        if not w:
            return True
        start, end = self.window_bounds(now if now is not None else self.clock())
        return start <= (now if now is not None else self.clock()) < end

    def window_bounds(self, now: float) -> tuple[float, float]:
        """(open, close) of the window we are in, or else of the next one."""
        return window_bounds(self.cfg.copy.window, self.cfg.copy.window_timezone, now)

    def _window_tick(self, now: float) -> None:
        if not self.cfg.copy.window or not self.equity_now:
            return
        st = self.journal.get(f"window:{self.mode}") or {}
        open_now = self.in_window(now)
        label = self.cfg.copy.window.replace("-", " → ")
        if open_now and not st.get("open") and self._pending_lineup is None:  # once Grok has said who we follow
            start, end = self.window_bounds(now)
            self.journal.put(f"window:{self.mode}", {"open": True, "opened_t": now, "start": start, "end": end,
                                                     "start_equity": self.equity_now})
            who = " and ".join(f"@{t.handle} (#{t.rank})" for t in self.rot.follows) or "nobody yet"
            self._feed("WINDOW", f"Copy window open · {label} · following {who} · risk cap "
                                 f"{self.cfg.limits.max_position_pct:.0%} per trade · keep the Mac on charge", alert=True)
        elif not open_now and st.get("open"):
            closed = [c for c in self.journal.closed_copies(self.mode) if (c["closed_t"] or 0) >= st["opened_t"]]
            wins = sum(1 for c in closed if (c["pnl"] or 0) > 0)
            start_eq, eq = float(st.get("start_equity") or self.equity_now), self.equity_now
            rate = f" · {wins / len(closed):.0%} win rate" if closed else ""
            self._feed("WINDOW", f"Copy window closed · ${start_eq:,.2f} → ${eq:,.2f} · {eq / start_eq:.1f}x · "
                                 f"{wins}W {len(closed) - wins}L{rate}", eq - start_eq, alert=True)
            self.journal.put(f"window:{self.mode}", {**st, "open": False, "closed_t": now, "end_equity": eq, "wins": wins,
                                                     "losses": len(closed) - wins})
            if self.cfg.copy.close_at_window_end:
                for c in self.journal.open_copies(self.mode):
                    self.sell(c, 1.0, "copy window closed")

    def _digest_tick(self, now: float) -> None:
        mins = self.cfg.notify.digest_minutes
        if not mins:
            return
        last = float(self.journal.get(f"digest_at:{self.mode}") or 0)
        if not last:
            self.journal.put(f"digest_at:{self.mode}", now)
            return
        if now - last < mins * 60:
            return
        self.journal.put(f"digest_at:{self.mode}", now)
        done = [c for c in self.journal.closed_copies(self.mode) if (c["closed_t"] or 0) > last]
        if not done:
            return
        wins = sum(1 for c in done if (c["pnl"] or 0) > 0)
        parts = " · ".join(f"${c['symbol']} {'+' if (c['pnl'] or 0) >= 0 else '-'}${abs(c['pnl'] or 0):,.2f}" for c in done[-6:])
        self._feed("DIGEST", f"Copied {len(done)} trade{'s' if len(done) > 1 else ''} · {wins}W {len(done) - wins}L · {parts}",
                   round(sum(c["pnl"] or 0 for c in done), 2), alert=True)

    def holder_wallets(self) -> set[str]:
        """Traders whose trades we must not miss: we hold a coin they bought, and copy their exit."""
        return {c["trader"] for c in self.journal.open_copies(self.mode)}

    def watched_wallets(self) -> set[str]:
        return self.holder_wallets() | {t.wallet for t in self.rot.active(self.clock())}

    def _trader(self, wallet: str) -> Trader | None:
        lineup = self.rot.s["lineup"]
        if lineup is not self._by_wallet_src:
            self._by_wallet = {t["wallet"]: Trader(**t) for t in lineup}
            self._by_wallet_src = lineup
        return self._by_wallet.get(wallet)

    def _copying(self, wallet: str) -> bool:
        """Do we copy this wallet's buys right now?"""
        if not self.cfg.copy.follow_all:
            return wallet in self.rot.s["follows"]
        return self._trader(wallet) is not None and not self.rot.is_fired(wallet, self.clock())

    # ------------------------------------------------------------ one step
    def step(self) -> None:
        if self._jobs:
            self._run_jobs()
        self.refresh_lineup()
        self._window_tick(self.clock())  # before this step's trades: "window open" comes before its first copy
        try:
            if hasattr(self.watcher, "priority"):
                self.watcher.priority = self.holder_wallets()
            self.watcher.watch(self.watched_wallets())
            swaps = self.watcher.poll()
            self._api_ok("watch")
        except Exception as e:
            self._api_error(f"watching traders: {type(e).__name__}: {e}", "watch")
            swaps = []
        for sw in swaps:
            try:
                self.on_trade(sw)
            except Exception as e:  # one bad token must never stop the loop
                log.exception("Handling %s failed", sw.sig)
                self._api_error(f"handling trade: {type(e).__name__}: {e}", "trade")
        if self.clock() - self._last_mark >= MARK_SECONDS:
            self._last_mark = self.clock()
            self.mark()
            self._window_tick(self.clock())
            self._digest_tick(self.clock())
        self._api_ok("general")  # the whole step ran: a streak of loop crashes is broken

    def on_trade(self, sw) -> None:
        now = self.clock()
        self._signals.append(now)
        while self._signals and self._signals[0] < now - 3600:
            self._signals.popleft()
        if sw.side == "buy":
            if self._copying(sw.wallet):
                self.copy_buy(sw)
            return
        if not self.cfg.copy.exit_with_trader:
            return
        for c in self.journal.open_copies(self.mode):
            if c["trader"] == sw.wallet and c["mint"] == sw.mint:
                handle = c["handle"]
                self._feed("SIG", f"@{handle} sold {sw.sold_fraction:.0%} of ${c['symbol']}")
                if sw.t:
                    self._exit_lag[c["id"]] = max(0.0, now - sw.t)
                self.sell(c, sw.sold_fraction, "trader sold")

    # ------------------------------------------------------------ buying
    def _skip(self, sw, symbol: str, why: str) -> None:
        t = self._trader(sw.wallet)
        text = f"@{t.handle if t else sw.wallet[:6]} bought ${symbol} · not copied: {why}"
        self._tally(re.sub(r"[$\d][\d,.]*[%KMBkmbs]*", "N", why))
        now = self.clock()
        if self.cfg.copy.follow_all or self.cfg.copy.follow_count > 1:
            quiet = now - self._last_skip[1] < 20  # hundreds of traders: the feed shows a skip every 20 s, the tally counts them all
        else:
            quiet = why == self._last_skip[0] and now - self._last_skip[1] < 600
        if quiet:
            log.info("[SKIP] %s", text)  # same reason again (daily limit, kill switch): keep the feed readable
            return
        self._last_skip = (why, now)
        self._feed("SKIP", text)

    def _tally(self, reason: str) -> None:
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        rec = self.journal.get(f"skips:{self.mode}") or {}
        if rec.get("day") != day:
            rec = {"day": day, "n": {}}
        rec["n"][reason] = rec["n"].get(reason, 0) + 1
        if len(rec["n"]) > 60:  # reasons carry numbers: keep the table small
            rec["n"] = dict(sorted(rec["n"].items(), key=lambda kv: -kv[1])[:40])
        self.journal.put(f"skips:{self.mode}", rec)

    def copy_buy(self, sw) -> None:
        cfg, lim, now = self.cfg, self.cfg.limits, self.clock()
        trader = self._trader(sw.wallet)
        symbol = sw.mint[:4]
        if not self.in_window(now):  # outside the copy window: not even a SKIP line, only the count
            self._tally("outside the copy window")
            log.info("@%s bought %s outside the copy window: not copied", trader.handle if trader else sw.wallet[:6], sw.mint[:6])
            return
        if self.kill.tripped:
            return self._skip(sw, symbol, "kill switch is on")
        if sw.t and now - sw.t > cfg.copy.max_signal_age_seconds:
            return self._skip(sw, symbol, f"seen {now - sw.t:.0f}s late")
        if self.journal.get(f"daily_stop:{self.mode}") == datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"):
            return self._skip(sw, symbol, "daily loss limit hit; copying again after 00:00 UTC")
        opens = self.journal.open_copies(self.mode)
        holding = next((c for c in opens if c["mint"] == sw.mint), None)
        if holding is None and len(opens) >= lim.max_open_positions:
            return self._skip(sw, symbol, f"already {len(opens)} open copies")
        if holding is not None and holding["trader"] != sw.wallet and (cfg.copy.follow_all or cfg.copy.follow_count > 1):
            return self._skip(sw, symbol, "already copied another trader into it")
        if cfg.copy.only_first_buy and sw.pre_raw > 0:
            return self._skip(sw, symbol, "trader is adding to a token they already held")
        last = self.journal.last_exit(self.mode, sw.mint)
        if holding is None and last and now - last < cfg.copy.rebuy_cooldown_hours * 3600:
            return self._skip(sw, symbol, "sold it recently")
        if self.mode == "live" and self.sol_now is not None and self.sol_now < lim.min_sol_for_fees:
            self._alert(f"Bot wallet has under {lim.min_sol_for_fees} SOL for fees: send it some SOL.")
            return self._skip(sw, symbol, "not enough SOL for network fees")

        # size: the trader's buy x copy_ratio, inside every cap
        equity = self.equity_now or self.broker.usdc()
        open_cost = sum(c["cost_usd"] * self.remaining(c) / c["raw"] for c in opens if c["raw"])
        held_cost = (holding["cost_usd"] * self.remaining(holding) / holding["raw"]) if holding and holding["raw"] else 0.0
        usd = min(sw.usd * cfg.copy.copy_ratio, cfg.copy.max_trade_usd, equity * lim.max_position_pct - held_cost,
                  equity * lim.max_exposure_pct - open_cost, self.broker.usdc())
        if usd < cfg.copy.min_trade_usd:
            room_exposure = equity * lim.max_exposure_pct - open_cost
            room_position = equity * lim.max_position_pct - held_cost
            if room_exposure < cfg.copy.min_trade_usd:
                why = f"exposure limit: ${open_cost:,.0f} of ${equity * lim.max_exposure_pct:,.0f} allowed in memecoins is already in use"
            elif room_position < cfg.copy.min_trade_usd:
                why = f"already at the ${equity * lim.max_position_pct:,.0f} limit for one coin"
            elif self.broker.usdc() < cfg.copy.min_trade_usd:
                why = f"only ${self.broker.usdc():,.2f} USDC left"
            else:
                why = f"copy size ${max(usd, 0):.2f} under the ${cfg.copy.min_trade_usd:g} minimum"
            return self._skip(sw, symbol, why)
        usd = round(usd, 2)

        veto = self._token_veto.get(sw.mint)
        if veto and veto[0] > now:
            return self._skip(sw, symbol, veto[1])
        info = self._mint(sw.mint)
        cached = self._dex_cache.get(sw.mint)
        if cached and now - cached[0] < DEX_CACHE_SECONDS:
            stats = cached[1]
        else:
            stats = self.dex.get(sw.mint)
            self._dex_cache[sw.mint] = (now, stats)
            if len(self._dex_cache) > 500:
                self._dex_cache = {m: v for m, v in self._dex_cache.items() if now - v[0] < DEX_CACHE_SECONDS}
        symbol = stats.symbol if stats else symbol
        bad = token_vetoes(info, stats, cfg.safety)
        if bad:
            self._token_veto[sw.mint] = (now + TOKEN_VETO_SECONDS, "; ".join(bad))
            if len(self._token_veto) > 2000:
                self._token_veto = {m: v for m, v in self._token_veto.items() if v[0] > now}
            return self._skip(sw, symbol, "; ".join(bad))
        try:
            q = self.broker.quote_buy(sw.mint, usd)
        except ValueError as e:
            return self._skip(sw, symbol, f"no buy route ({e})")
        back = None
        if cfg.safety.check_sell_route:
            try:
                back = self.broker.quote_sell(sw.mint, q.raw)
            except ValueError:
                back = None
        bad = quote_vetoes(q, back, info.decimals, sw.price, cfg.safety, cfg.copy.max_chase_pct)
        if bad:
            return self._skip(sw, symbol, "; ".join(bad))

        fill = self._execute(q)
        if fill is None or fill.raw <= 0:
            return
        if holding:
            self.journal.update_copy(holding["id"], raw=holding["raw"] + fill.raw, cost_usd=holding["cost_usd"] + fill.usd, last_ref=fill.ref)
        else:
            self.journal.open_copy(mode=self.mode, mint=sw.mint, symbol=symbol, decimals=info.decimals, trader=sw.wallet,
                                   handle=trader.handle if trader else sw.wallet[:6], opened_t=int(now), cost_usd=fill.usd,
                                   raw=fill.raw, trader_price=sw.price, trader_sig=sw.sig, buy_ref=fill.ref)
        self.marks[sw.mint] = fill.usd / (fill.raw / 10 ** info.decimals)
        self._copies.append(now)
        while self._copies and self._copies[0] < now - 3600:
            self._copies.popleft()
        rank = f" · weekly top #{trader.rank}" if trader else ""
        capped = " · inside the risk cap" if fill.usd < sw.usd * cfg.copy.copy_ratio - 0.01 else ""
        self._feed("COPY", f"@{trader.handle if trader else '?'} bought ${symbol} · ${sw.usd:,.0f} in · copy {cfg.copy.copy_ratio:g}x · "
                           f"mirrored with ${fill.usd:,.2f}{capped}{rank}", alert=True)
        if self.grok and cfg.grok.x_check and fill.usd >= cfg.grok.x_check_min_usd and self.grok.available()[0]:
            self._submit("xcheck", self.grok.check_token, sw.mint, context={"mint": sw.mint, "symbol": symbol})

    def _execute(self, q: Quote) -> Fill | None:
        """Send it. On an unknown outcome, read the wallet to learn what really happened."""
        before_tok = before_usd = None
        if self.mode == "live":
            try:
                h = self.broker.holdings()
                before_tok, before_usd = h.get(q.mint, 0), h.get(USDC, 0) / 1e6
            except Exception:
                pass
        try:
            fill = self.broker.execute(q)
            self._api_ok("swap")
            return fill
        except SwapOutcomeUnknown as e:
            if before_tok is None:
                self._api_error(f"swap: {e}", "swap")
                return None
            for _ in range(6):  # give the RPC a moment to see the landed swap
                time.sleep(5)
                try:
                    tok, usdc = self.broker.token_raw(q.mint), self.broker.usdc()
                except Exception:
                    continue
                d_tok, d_usd = tok - before_tok, usdc - before_usd
                if q.side == "buy" and d_tok > 0:
                    return Fill("buy", q.mint, d_tok, -d_usd if d_usd < 0 else q.usd, "reconciled-from-wallet")
                if q.side == "sell" and d_tok < 0:
                    return Fill("sell", q.mint, -d_tok, max(d_usd, 0.0), "reconciled-from-wallet")
            self._api_error(f"swap outcome unknown and the wallet shows no change: {e}", "swap")
            return None
        except Exception as e:
            self._api_error(f"swap: {type(e).__name__}: {e}", "swap")
            return None

    # ------------------------------------------------------------ selling
    def sell(self, c, fraction: float, reason: str) -> bool:
        """Sell this share of what's left of the copy. Returns False if it must be retried."""
        c = self.journal.copy(c["id"])
        left = self.remaining(c)
        fraction = max(0.0, min(1.0, fraction))
        if left <= 0 or fraction <= 0:
            return True
        raw = left if fraction >= 0.999 else int(left * fraction)
        price = self.marks.get(c["mint"]) or (c["cost_usd"] / (c["raw"] / 10 ** c["decimals"]))
        if (left - raw) / 10 ** c["decimals"] * price < DUST_USD:
            raw = left  # don't leave crumbs behind
        if self.mode == "live":
            try:
                raw = min(raw, self.broker.token_raw(c["mint"]))  # never sell more than the wallet holds
            except Exception as e:
                self._api_error(f"token balance: {e}", "wallet")
        if raw <= 0:
            return self._finish(c, 0, 0.0, reason + " (wallet already empty)", "none")
        try:
            q = self.broker.quote_sell(c["mint"], raw)
            fill = self._execute(q)
        except Exception as e:  # no route, Jupiter down, rate limited: retried at the next check
            fill = None
            self._feed("WARN", f"can't sell ${c['symbol']} yet: {type(e).__name__}: {e}")
        if fill is None:
            n = self._sell_failures[c["id"]] = self._sell_failures.get(c["id"], 0) + 1
            if n >= WRITE_OFF_AFTER and self.marks.get(c["mint"], 1.0) * left / 10 ** c["decimals"] < DUST_USD * 10:
                self._sell_failures.pop(c["id"], None)
                return self._finish(c, 0, 0.0, "no way to sell it: written off as worthless", "none")
            self.journal.update_copy(c["id"], exit_pending=max(float(c["exit_pending"] or 0), fraction))
            self._alert(f"SELL of ${c['symbol']} failed ({reason}); retrying every {MARK_SECONDS}s.")
            return False
        self._sell_failures.pop(c["id"], None)
        return self._finish(c, fill.raw, fill.usd, reason, fill.ref)

    def _finish(self, c, sold_raw: int, usd: float, reason: str, ref: str) -> bool:
        sold_total = int(c["sold_raw"]) + sold_raw
        sold_usd = float(c["sold_usd"]) + usd
        closed = sold_total >= int(c["raw"]) or sold_raw == 0
        upd = {"sold_raw": sold_total, "sold_usd": sold_usd, "exit_pending": 0, "last_ref": ref}
        now = self.clock()
        part_pnl = usd - c["cost_usd"] * (sold_raw / c["raw"]) if c["raw"] else 0.0
        if not closed:
            self.journal.update_copy(c["id"], **upd)
            self._feed("EXIT", f"sold {sold_raw / c['raw']:.0%} of ${c['symbol']} ({reason})", part_pnl)
            return True
        pnl = sold_usd - c["cost_usd"]
        self.journal.update_copy(c["id"], **upd, closed_t=int(now), pnl=pnl, reason=reason)
        pct = pnl / c["cost_usd"] * 100 if c["cost_usd"] else 0.0
        lag = self._exit_lag.pop(c["id"], None)
        held = f"held {max(0, (now - c['opened_t']) / 60):.0f}m"
        t = self._trader(c["trader"])
        if pct >= self.cfg.notify.big_win_pct:
            src = f"copied from weekly top #{t.rank} · @{c['handle']}" if t else f"copied from @{c['handle']}"
            after = f" · exit {lag:.1f}s after the trader sold" if lag is not None else f" · {reason}"
            self._feed("BIG", f"Big win · sold ${c['symbol']} · +${pnl:,.2f} (+{pct:.0f}%) · {src}{after} · {held}", pnl, alert=True)
        else:
            self._feed("EXIT", f"@{c['handle']} ${c['symbol']} · {reason} · we exit ({pct:+.0f}%, {held})", pnl,
                       alert=not self.cfg.notify.digest_minutes)
        before = set(self.rot.s["follows"])
        fired, events = self.rot.record(c["trader"], pnl, now)
        switching = self.grok is not None or self.cfg.copy.follow_count > 1
        for e in events:
            self._feed("FIRE" if "fired" in e else "PICK", e, alert=not switching)
        if fired and switching and c["trader"] in before:
            new = [x for x in self.rot.follows if x.wallet not in before]
            now_copying = ", ".join(f"weekly top #{x.rank} @{x.handle}" + (f" · {x.win_rate:.0%} win rate" if x.win_rate is not None else "")
                                    for x in new) or "nobody new (everyone on the list is fired)"
            who = "Grok switched trader" if self.grok else "Switched trader"
            self._feed("SWITCH", f"{who} · @{c['handle']} took {self.cfg.copy.fire_after_losses} losses in a row · now copying {now_copying}",
                       alert=True)
            if self._wants_picks():  # the records changed: let Grok re-order the bench for the next switch
                self._ask_picks(self.rot.lineup)
        self._save_rotation()
        if fired and not self.rot.active(now):
            self._alert("Every trader on the weekly top is fired. Copying stops until the list refreshes.")
        return True

    # ------------------------------------------------------------ the 15-second check
    def equity(self) -> float:
        opens = self.journal.open_copies(self.mode)
        mints = [c["mint"] for c in opens]
        if mints:
            try:
                self.marks.update(self.prices.get(mints))
            except Exception as e:
                log.warning("Price check failed: %s", type(e).__name__)
        value = 0.0
        for c in opens:
            p = self.marks.get(c["mint"]) or c["cost_usd"] / (c["raw"] / 10 ** c["decimals"])
            value += self.remaining(c) / 10 ** c["decimals"] * p
        return self.broker.usdc() + value

    def mark(self) -> None:
        now = self.clock()
        try:
            eq = self.equity()
            self._api_ok("wallet")
        except Exception as e:
            return self._api_error(f"wallet: {type(e).__name__}: {e}", "wallet")
        self.equity_now = eq
        self.journal.put(f"equity:{self.mode}", eq)
        self.journal.equity(self.mode, now, eq)
        if self.journal.get(f"start:{self.mode}") is None:
            self.journal.put(f"start:{self.mode}", eq)
        peak = max(float(self.journal.get(f"peak:{self.mode}") or eq), eq)
        self.journal.put(f"peak:{self.mode}", peak)
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.journal.get(f"day:{self.mode}") != day:
            self.journal.put(f"day:{self.mode}", day)
            self.journal.put(f"day_start:{self.mode}", eq)
        day_start = float(self.journal.get(f"day_start:{self.mode}") or eq)
        lim = self.cfg.limits

        if self.kill.tripped:
            for c in self.journal.open_copies(self.mode):
                self.sell(c, 1.0, "kill switch")
            return
        if self.api_errors and not self.journal.get(f"api_errors:{self.mode}"):
            self.errors = {k: 0 for k in self.errors}  # ./fomo.sh reset cleared the streak: start counting afresh
        if peak > 0 and (peak - eq) / peak >= lim.max_drawdown_pct:
            return self.trip(f"equity fell {(peak - eq) / peak:.0%} from its high (limit {lim.max_drawdown_pct:.0%})")
        if day_start > 0 and (day_start - eq) / day_start >= lim.daily_loss_limit_pct and self.journal.get(f"daily_stop:{self.mode}") != day:
            self.journal.put(f"daily_stop:{self.mode}", day)
            self._feed("STOP", f"down {(day_start - eq) / day_start:.0%} today: no new copies until 00:00 UTC", alert=True)

        ex = self.cfg.exits
        for c in self.journal.open_copies(self.mode):
            cost_px = c["cost_usd"] / (c["raw"] / 10 ** c["decimals"])
            p = self.marks.get(c["mint"], cost_px)
            chg = (p / cost_px - 1) * 100
            if c["exit_pending"]:
                self.sell(c, float(c["exit_pending"]), "retrying the trader's exit")
            elif ex.stop_loss_pct is not None and chg <= -ex.stop_loss_pct:
                self.sell(c, 1.0, f"stop loss {chg:.0f}%")
            elif ex.take_profit_pct is not None and chg >= ex.take_profit_pct:
                self.sell(c, 1.0, f"take profit +{chg:.0f}%")
            elif ex.max_hold_hours is not None and now - c["opened_t"] >= ex.max_hold_hours * 3600:
                self.sell(c, 1.0, f"held {ex.max_hold_hours:g}h")
        if self.mode == "live":
            try:
                self.sol_now = self.broker.sol()
            except Exception as e:
                self._api_error(f"SOL balance: {e}", "wallet")
            self._reconcile_wallet()

    def _reconcile_wallet(self) -> None:
        """If tokens left the wallet without the bot (you sold by hand), stop tracking what isn't there."""
        try:
            held = self.broker.holdings()
        except Exception as e:
            return self._api_error(f"holdings: {e}", "wallet")
        for c in self.journal.open_copies(self.mode):
            have, want = held.get(c["mint"], 0), self.remaining(c)
            if have < want * 0.95:
                self._feed("WARN", f"wallet holds less ${c['symbol']} than the bot expected: tracking {have} instead of {want}")
                if have == 0:
                    self._finish(self.journal.copy(c["id"]), 0, 0.0, "tokens left the wallet outside the bot", "none")
                else:
                    self.journal.update_copy(c["id"], raw=int(c["sold_raw"]) + have, cost_usd=c["cost_usd"] * (int(c["sold_raw"]) + have) / c["raw"])

    # ------------------------------------------------------------ kill switch
    def trip(self, reason: str) -> None:
        self.kill.trip(reason)
        self._feed("KILL", f"{reason} · selling everything and stopping", alert=True)
        for c in self.journal.open_copies(self.mode):
            self.sell(c, 1.0, "kill switch")

    # ------------------------------------------------------------ dashboard
    def dashboard_state(self) -> dict:
        now = self.clock()
        start = float(self.journal.get(f"start:{self.mode}") or self.equity_now or 0)
        eq = self.equity_now
        cur = self.rot.following
        st = self.rot.stats(cur.wallet) if cur else None
        curve = [(r["t"], r["v"]) for r in self.journal.equity_curve(self.mode)]
        if len(curve) > 600:
            step = len(curve) / 600
            curve = [curve[int(i * step)] for i in range(600)] + [curve[-1]]
        crowd = self.cfg.copy.follow_all
        lineup = []
        for t in self.rot.lineup:
            s = self.rot.stats(t.wallet)
            fired = self.rot.is_fired(t.wallet, now)
            status = "fired" if fired else "following" if (crowd or (cur and t.wallet == cur.wallet)) else "bench"
            lineup.append({"rank": t.rank, "handle": t.handle, "wallet": t.wallet, "pnl7d": t.pnl_7d, "winRate": t.win_rate,
                           "status": status, "us": round(s["pnl"], 2), "trades": s["trades"]})
        n_listed, n_fired = len(lineup), sum(1 for r in lineup if r["status"] == "fired")
        if crowd:  # 500 rows don't fit a page: the ones we have traded with, then the best ranked
            lineup = sorted(lineup, key=lambda r: (-r["trades"], r["rank"]))[:40]
        scored = sorted((r for r in lineup if r["trades"]), key=lambda r: r["us"])
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        skips = self.journal.get(f"skips:{self.mode}") or {}
        crowd_state = {
            "on": crowd, "listed": n_listed, "fired": n_fired, "followed": n_listed - n_fired,
            "signals1h": len(self._signals), "copies1h": len(self._copies),
            "worst": scored[:5], "best": scored[::-1][:5],
            "skips": sorted(({"reason": k, "n": v} for k, v in (skips.get("n") or {}).items()), key=lambda r: -r["n"])[:8] if skips.get("day") == day else [],
            "watch": getattr(self.watcher, "status", lambda: "")(),
        }
        positions = []
        for c in self.journal.open_copies(self.mode):
            cost_px = c["cost_usd"] / (c["raw"] / 10 ** c["decimals"])
            p = self.marks.get(c["mint"], cost_px)
            left = self.remaining(c) / c["raw"] if c["raw"] else 0
            positions.append({"symbol": c["symbol"], "mint": c["mint"], "handle": c["handle"], "cost": round(c["cost_usd"] * left, 2),
                              "value": round(self.remaining(c) / 10 ** c["decimals"] * p, 2), "pnlPct": round((p / cost_px - 1) * 100, 1),
                              "ageMin": int((now - c["opened_t"]) / 60)})
        closed = self.journal.closed_copies(self.mode)
        results = {"n": len(closed), "wins": sum(1 for c in closed if (c["pnl"] or 0) > 0), "pnl": round(sum(c["pnl"] or 0 for c in closed), 2)}
        window = None
        if self.cfg.copy.window:
            wst = self.journal.get(f"window:{self.mode}") or {}
            is_open = self.in_window(now)
            start, end = self.window_bounds(now)
            since = start if is_open else wst.get("opened_t", start)
            done = [c for c in closed if (c["closed_t"] or 0) >= since]
            window = {"label": self.cfg.copy.window.replace("-", " → "), "open": is_open, "start": start, "end": end,
                      "startEquity": wst.get("start_equity"), "endEquity": None if is_open else wst.get("end_equity"),
                      "wins": sum(1 for c in done if (c["pnl"] or 0) > 0),
                      "losses": sum(1 for c in done if (c["pnl"] or 0) <= 0),
                      "copies": [c["opened_t"] for c in [*closed, *self.journal.open_copies(self.mode)] if c["opened_t"] >= since]}
        follows = []
        for t in self.rot.follows if not crowd else []:
            s = self.rot.stats(t.wallet)
            follows.append({"handle": t.handle, "wallet": t.wallet, "rank": t.rank, "winRate": t.win_rate, "pnl7d": t.pnl_7d,
                            "us": round(s["pnl"], 2), "trades": s["trades"], "streak": s["streak"], "last": s["last"]})
        grok = {"on": self.grok is not None, "model": self.cfg.grok.model, "budget": self.cfg.grok.daily_budget_usd,
                "spent": round(self.grok.spent_today(), 4) if self.grok else 0.0, "xCheck": self.cfg.grok.x_check,
                "picks": self.cfg.grok.pick_traders, "why": "" if self.grok else ("off in config" if not self.cfg.grok.enabled else "no XAI_API_KEY")}
        return {
            "mode": self.mode, "now": now, "killed": self.kill.tripped, "killReason": self.kill.reason, "results": results,
            "wsConnected": bool(getattr(self.watcher, "ws_connected", False)),
            "balance": round(eq, 2), "start": round(start, 2), "pnl": round(eq - start, 2),
            "pnlPct": round((eq / start - 1) * 100, 1) if start else 0.0, "multiple": round(eq / start, 2) if start else 1.0,
            "tradesCopied": self.journal.count_copies(self.mode),
            "following": ({"handle": cur.handle, "wallet": cur.wallet, "rank": cur.rank, "pnl7d": cur.pnl_7d, "winRate": cur.win_rate,
                           "copyRatio": self.cfg.copy.copy_ratio, "last6": st["last"], "withHim": round(st["pnl"], 2)} if cur else None),
            "fireRule": {"after": self.cfg.copy.fire_after_losses, "streak": st["streak"] if st else 0, "firedSoFar": self.rot.s["fires"]},
            "equity": curve, "stints": self.rot.s["stints"], "lineup": lineup, "positions": positions, "crowd": crowd_state,
            "window": window, "follows": follows, "grok": grok,
            "feed": [{"t": r["t"], "kind": r["kind"], "text": r["text"], "pnl": r["pnl"]} for r in self.journal.events(self.mode, 80)],
            "dailyStop": self.journal.get(f"daily_stop:{self.mode}") == datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
            "limits": {"maxTrade": self.cfg.copy.max_trade_usd, "maxOpen": self.cfg.limits.max_open_positions, "copyRatio": self.cfg.copy.copy_ratio,
                       "riskCap": self.cfg.limits.max_position_pct, "fireAfter": self.cfg.copy.fire_after_losses,
                       "stopLoss": self.cfg.exits.stop_loss_pct, "maxDrawdown": self.cfg.limits.max_drawdown_pct},
        }
