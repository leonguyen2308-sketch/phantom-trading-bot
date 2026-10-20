"""The desk.

SCANNER (its own thread; slow work): sweep the universe -> G1 market -> G2 on-chain -> G3 quote -> G4 candles ->
exit plan -> shadow position -> Claude seats -> CHIEF (code) -> buy.
MANAGER (main thread, every second): prices every 10 s for open positions (real and shadow), the exit plan,
equity, hard limits, the kill switch. A slow desk review never delays a stop.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone

from desk import funnel
from desk.ai import DeskUnavailable, sanitize_symbol
from desk.features import LEGEND, candle_fields, market_fields, snapshot
from desk.journal import day_of, trade_stats
from desk.plan import ExitPlan, make_plan, step as plan_step
from fomo.brokers import Fill
from fomo.swaps import USDC
from phantom.execution.jupiter import SwapOutcomeUnknown

log = logging.getLogger(__name__)

PRICE_SECONDS = 10
VOLUME_SECONDS = 300
MARK_SECONDS = 15
DUST_USD = 0.5
WRITE_OFF_AFTER = 30
ALERT_REPEAT_SECONDS = 300
SEATS = ("SCOUT", "ANALYST", "NEWS", "SKEPTIC", "CHIEF")


class Desk:
    def __init__(self, *, mode: str, cfg, broker, jup, dex, gecko, rug, holders, mint_info, prices, desk, journal, kill,
                 alerts=None, watcher=None, tracked: dict | None = None, clock=time.time, threaded: bool = True):
        self.mode, self.cfg, self.broker = mode, cfg, broker
        self.jup, self.dex, self.gecko, self.rug, self.holders, self.mint_info_fn, self.prices = jup, dex, gecko, rug, holders, mint_info, prices
        self.desk, self.journal, self.kill, self.alerts, self.clock = desk, journal, kill, alerts, clock
        self.watcher, self.tracked = watcher, tracked or {}
        self.threaded = threaded
        self.lock = threading.RLock()
        self.queue: queue.Queue = queue.Queue()
        self.errors: dict[str, int] = {"general": int(journal.get(f"api_errors:{mode}", 0))}
        self._error_window: dict[str, float] = {}
        self._alerted: dict[str, float] = {}
        self._mints: dict[str, object] = {}
        self._cool: dict[str, float] = {}
        self._counted: dict[tuple[str, str], set] = {}
        self._reviews: deque[float] = deque()
        self._sell_failures: dict[int, int] = {}
        self.prices_now: dict[str, float] = {}
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
        if self.alerts:
            try:
                self.alerts.send(f"[{self.mode.upper()}] {text}")
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
        self.journal.put(f"api_errors:{self.mode}", self.api_errors)
        log.warning("API error %d (%s): %s", self.errors[source], source, what)
        if self.errors[source] >= self.cfg.risk.max_consecutive_api_errors and not self.kill.tripped:
            self.trip(f"{self.errors[source]} API errors in a row ({source}; last: {what})")

    def _api_ok(self, source: str) -> None:
        if self.errors.get(source):
            self.errors[source] = 0
            self.journal.put(f"api_errors:{self.mode}", self.api_errors)

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
        if mint not in self._mints:
            self._mints[mint] = self.mint_info_fn(mint)
        return self._mints[mint]

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
        lists = [("toptrending", "5m")]
        if self.sweeps % max(sc.organic_every, 1) == 1 or sc.organic_every <= 1:
            lists.append(("toporganicscore", "1h"))
        for kind, window in lists:
            try:
                for t in self.jup.top(kind, window, 100):
                    universe.setdefault(t.get("id"), t)
            except Exception as e:
                self._api_error(f"Jupiter {kind}: {type(e).__name__}", "jupiter")
        try:
            for t in self.jup.recent():
                universe.setdefault(t.get("id"), t)
        except Exception as e:
            log.info("Jupiter recent unavailable: %s", type(e).__name__)
        if self.sweeps % max(sc.dexscreener_every, 1) == 1 or sc.dexscreener_every <= 1:
            for m in self.dex.discovered():
                universe.setdefault(m, None)
        universe.pop(None, None)
        universe.pop(USDC, None)
        mints = list(universe)[: sc.max_universe]
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
            self._api_error(f"DexScreener: {type(e).__name__}", "dexscreener")
            self.scan_state = "idle"
            return
        survivors = []
        for m in mints:
            self._count("universe", m)
            jt, dx = universe.get(m), pairs.get(m)
            mf = market_fields(jt, dx, now)
            why = funnel.g1_market(mf, (dx or {}).get("dexId", ""), jt, self.cfg.gates)
            if why:
                self._kill("market", m, why, cool=False)
                continue
            self._count("market", m)
            survivors.append((m, jt, dx, mf))
        held = {p["mint"] for p in self.journal.open_positions(self.mode)}
        ready = [s for s in survivors if self._cool.get(s[0], 0) <= now and s[0] not in held]
        ready.sort(key=lambda s: ((s[3].get("vol_accel") or 0) * (s[3].get("organic_score") or 50)), reverse=True)
        checking = min(len(ready), sc.deep_checks_per_sweep)
        if checking or self.sweeps % 15 == 1:
            self._feed("SCAN", "SCOUT", f"swept {len(mints)} tokens · {len(survivors)} pass the market gate · checking {checking} more closely")
        for m, jt, dx, mf in ready[: sc.deep_checks_per_sweep]:
            if self._stop.is_set():
                break
            self.evaluate(m, "scout", jt, dx, mf)
        self.scan_state = "idle"

    def _evaluate_mint(self, mint: str, station: str, source_wallet: str | None = None) -> None:
        """A TRACK signal: fetch the market numbers for one mint, then run the same funnel."""
        now = self.clock()
        if self._cool.get(mint, 0) > now or any(p["mint"] == mint for p in self.journal.open_positions(self.mode)):
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
        with self.lock:
            opens = self.journal.open_positions(self.mode)
            try:
                cash = self.broker.usdc()
            except Exception as e:
                return 0.0, f"wallet unreadable ({type(e).__name__})"
        if len(opens) >= r.max_open_positions:
            return 0.0, f"already {len(opens)} open positions"
        eq = self.equity_now or cash
        open_cost = sum(p["cost_usd"] * (p["raw"] - p["sold_raw"]) / p["raw"] for p in opens if p["raw"])
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

        # G2: on-chain facts
        try:
            info = self._mint_info(mint)
            top = self.holders.top(mint)
            fresh, _ = self.holders.fresh_share(top, cfg.holders.fresh_wallet_hours)
            self._api_ok("rpc")
        except Exception as e:
            self._api_error(f"RPC: {type(e).__name__}: {e}", "rpc")
            return "error"
        rug = self.rug.summary(mint)
        hf = {"top1_pct": round(top[0][1], 3) if top else 0.0, "top10_pct": round(sum(p for _, p in top[:10]), 3),
              "fresh_share_pct": round(fresh, 3),
              "rugcheck_risk": float(rug["score_normalised"]) if rug and rug.get("score_normalised") is not None else None}
        why = funnel.g2_onchain(info, hf, rug, cfg.holders)
        if why:
            self._kill("onchain", mint, why, cool=True)
            self._feed("KILL", "SCOUT", f"${symbol} · on-chain: {'; '.join(why[:2])}")
            return "onchain"
        self._count("onchain", mint)

        # G3: a real quote for our ticket, and selling it straight back
        try:
            q = self.broker.quote_buy(mint, ticket)
        except ValueError as e:
            self._kill("depth", mint, [f"no buy route ({e})"], cool=True)
            return "depth"
        except Exception as e:
            self._api_error(f"quote: {type(e).__name__}", "jupiter")
            return "error"
        try:
            back = self.broker.quote_sell(mint, q.raw)
        except ValueError:
            back = None
        except Exception as e:
            self._api_error(f"quote: {type(e).__name__}", "jupiter")
            return "error"
        df, why = funnel.g3_depth(q, back, info.decimals, cfg.depth)
        if why:
            self._kill("depth", mint, why, cool=True)
            self._feed("KILL", "SCOUT", f"${symbol} · depth: {'; '.join(why[:2])}")
            return "depth"
        self._count("depth", mint)

        # G4: where it is in its move
        pool = (dx or {}).get("pairAddress") or ((jt or {}).get("firstPool") or {}).get("id")
        candles = []
        if pool:
            try:
                candles = self.gecko.candles(pool, 5, 72)
            except Exception as e:
                log.info("candles unavailable for %s: %s", symbol, type(e).__name__)
        cf = candle_fields(candles, now)
        why = funnel.g4_window(cf, len(candles), cfg.window)
        entry = q.usd / (q.raw / 10 ** info.decimals) if q.raw else 0.0
        plan, plan_why = make_plan(entry, cf.get("atr_5m_pct"), mf.get("vol_5m_usd") or 0.0, cfg.plan)
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
        self._feed("PICK", "SCOUT", f"${symbol} passed every gate ({station}) · mcap ${(snap.get('mcap_usd') or 0) / 1e6:.2f}M · "
                                    f"liq ${(snap.get('liquidity_usd') or 0) / 1e3:.0f}K · stop -{plan.stop_pct:.0f}%")

        # the desk
        opinion, desk_note = None, ""
        self._cool[mint] = now + cfg.scan.recheck_passed_minutes * 60
        while self._reviews and self._reviews[0] < now - 3600:
            self._reviews.popleft()
        if len(self._reviews) >= cfg.desk.max_reviews_per_hour:
            desk_note = f"desk busy ({cfg.desk.max_reviews_per_hour} reviews this hour)"
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
            if cfg.desk.when_unavailable == "rules_only":
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
            self._open_shadow(mint, symbol, info.decimals, station, pool, ticket, entry, plan, did)
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
        blocked = self._buys_blocked()
        if blocked:
            self._feed("SKIP", "CHIEF", f"${symbol}: {blocked}")
            return "blocked"
        try:
            q = self.broker.quote_buy(mint, size)
        except Exception as e:
            self._feed("SKIP", "CHIEF", f"${symbol}: re-quote failed ({type(e).__name__})")
            return "error"
        price = q.usd / (q.raw / 10 ** decimals) if q.raw else 0.0
        moved = (price / decided_price - 1) * 100 if decided_price else 0.0
        if moved > self.cfg.depth.max_requote_move_pct:
            self._feed("SKIP", "CHIEF", f"${symbol}: price ran {moved:.0f}% while the desk was deciding")
            return "ran"
        with self.lock:
            fill = self._execute(q, "buy")
            if fill is None or fill.raw <= 0:
                return "fill-failed"
            fill_price = fill.usd / (fill.raw / 10 ** decimals)
            plan, _ = make_plan(fill_price, atr, vol5, self.cfg.plan)
            if plan is None:  # the fill moved the levels so far the plan no longer passes: keep the decided one, re-based
                plan, _ = make_plan(decided_price, atr, vol5, self.cfg.plan)
            self.journal.open_position(mode=self.mode, book="real", mint=mint, symbol=symbol, decimals=decimals, station=station,
                                       pool=pool, opened_t=int(self.clock()), cost_usd=fill.usd, raw=fill.raw, entry_price=fill_price,
                                       plan=plan.to_dict(), decision_id=did, buy_ref=fill.ref, source_wallet=source_wallet)
            self.prices_now[mint] = fill_price
            self.journal.count(self.mode, self.clock(), "filled")
        self._feed("BUY", "CHIEF", f"${symbol} · ${fill.usd:,.2f} at {fill_price:.6g} · stop {plan.stop:.6g} (-{plan.stop_pct:.0f}%) · "
                                   f"TP {', '.join(f'+{p:g}%' for p in self.cfg.plan.ladder_pct)}", alert=True)
        return "buy"

    def _execute(self, q, side: str) -> Fill | None:
        """Send it; on an unknown outcome read the wallet to learn what really happened."""
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
            for _ in range(6):
                time.sleep(5)
                try:
                    h = self.broker.holdings()
                except Exception:
                    continue
                d_tok, d_usd = h.get(q.mint, 0) - before_tok, h.get(USDC, 0) / 1e6 - before_usd
                if side == "buy" and d_tok > 0:
                    return Fill("buy", q.mint, d_tok, -d_usd if d_usd < 0 else q.usd, "reconciled-from-wallet")
                if side == "sell" and d_tok < 0:
                    return Fill("sell", q.mint, -d_tok, max(d_usd, 0.0), "reconciled-from-wallet")
            self._api_error(f"swap outcome unknown and the wallet shows no change: {e}", "swap")
            return None
        except Exception as e:
            self._api_error(f"swap: {type(e).__name__}: {e}", "swap")
            return None

    # ---------------------------------------------------------------- shadow (rules-only arm)
    def _open_shadow(self, mint, symbol, decimals, station, pool, ticket, entry, plan: ExitPlan, did) -> None:
        cost = self.cfg.plan.round_trip_cost_pct / 2 / 100
        raw = int(ticket * (1 - cost) / entry * 10 ** decimals) if entry else 0
        if raw <= 0:
            return
        self.journal.open_position(mode=self.mode, book="shadow", mint=mint, symbol=symbol, decimals=decimals, station=station,
                                   pool=pool, opened_t=int(self.clock()), cost_usd=ticket, raw=raw, entry_price=entry,
                                   plan=plan.to_dict(), decision_id=did, buy_ref="shadow")

    # ================================================================ the manager (main thread)
    def step(self) -> None:
        now = self.clock()
        if not self.threaded:
            try:
                self.scan_once()
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
                    for p in self.journal.open_positions(self.mode):
                        if p["mint"] == sw.mint and p["source_wallet"] == sw.wallet:
                            self._feed("SIG", "SCOUT", f"tracked wallet {name} sold {sw.sold_fraction:.0%} of ${p['symbol']}")
                            self._sell(p, 1.0, "exit", f"the tracked wallet sold {sw.sold_fraction:.0%}")

    def manage(self, volume_sample: bool = False) -> None:
        with self.lock:
            real = self.journal.open_positions(self.mode, "real")
            shadow = self.journal.open_positions(self.mode, "shadow")
        mints = list(dict.fromkeys(p["mint"] for p in real + shadow))
        if not mints:
            return
        try:
            for i in range(0, len(mints), 50):
                self.prices_now.update(self.prices.get(mints[i:i + 50]))
            self._api_ok("prices")
        except Exception as e:
            self._api_error(f"prices: {type(e).__name__}", "prices")
        if volume_sample:
            self.last["volume"] = self.clock()
            try:
                for m, pair in self.dex.tokens(mints).items():
                    v = ((pair.get("volume") or {}).get("m5"))
                    if v is not None:
                        self.vol_now[m] = float(v)
            except Exception as e:
                log.info("volume sample failed: %s", type(e).__name__)
                volume_sample = False
        now = self.clock()
        with self.lock:
            for p in real + shadow:
                price = self.prices_now.get(p["mint"])
                if not price:
                    continue
                plan = ExitPlan.from_dict(json.loads(p["plan"]))
                left = (p["raw"] - p["sold_raw"]) / p["raw"] if p["raw"] else 0.0
                if self.kill.tripped and p["book"] == "real":
                    act = None
                    self._sell(p, 1.0, "kill", "kill switch")
                    continue
                if p["exit_pending"]:
                    self._sell(p, float(p["exit_pending"]), "retry", "retrying a failed sell")
                    continue
                act = plan_step(plan, price, self.vol_now.get(p["mint"]), now - p["opened_t"], left, volume_sample)
                self.journal.update_position(p["id"], plan=plan.to_dict())
                if act:
                    if p["book"] == "real":
                        self._sell(self.journal.position(p["id"]), act.fraction, act.kind, act.note)
                    else:
                        self._shadow_sell(self.journal.position(p["id"]), act.fraction, act.kind, price)

    def _shadow_sell(self, p, fraction: float, kind: str, price: float) -> None:
        left = p["raw"] - p["sold_raw"]
        raw = left if fraction >= 0.999 else int(left * fraction)
        usd = raw / 10 ** p["decimals"] * price * (1 - self.cfg.plan.round_trip_cost_pct / 2 / 100)
        self._finish(p, raw, usd, kind, "shadow")

    def _sell(self, p, fraction: float, kind: str, note: str) -> bool:
        p = self.journal.position(p["id"])
        left = p["raw"] - p["sold_raw"]
        if left <= 0:
            return True
        raw = left if fraction >= 0.999 else int(left * fraction)
        price = self.prices_now.get(p["mint"]) or p["entry_price"]
        if (left - raw) / 10 ** p["decimals"] * price < DUST_USD:
            raw = left
        if self.mode == "live":
            try:
                raw = min(raw, self.broker.token_raw(p["mint"]))
            except Exception as e:
                self._api_error(f"token balance: {e}", "wallet")
        if raw <= 0:
            return self._finish(p, 0, 0.0, kind, "wallet-empty", note="wallet already empty")
        fill = None
        try:
            fill = self._execute(self.broker.quote_sell(p["mint"], raw), "sell")
        except Exception as e:
            self._feed("WARN", "CHIEF", f"can't sell ${p['symbol']} yet: {type(e).__name__}: {e}")
        if fill is None:
            n = self._sell_failures[p["id"]] = self._sell_failures.get(p["id"], 0) + 1
            if n >= WRITE_OFF_AFTER and price * left / 10 ** p["decimals"] < DUST_USD * 10:
                return self._finish(p, 0, 0.0, kind, "none", note="no way to sell it: written off as worthless")
            self.journal.update_position(p["id"], exit_pending=max(float(p["exit_pending"] or 0), fraction))
            self._alert(f"SELL of ${p['symbol']} failed ({note}); retrying every {PRICE_SECONDS}s.")
            return False
        self._sell_failures.pop(p["id"], None)
        return self._finish(p, fill.raw, fill.usd, kind, fill.ref, note=note)

    def _finish(self, p, sold_raw: int, usd: float, kind: str, ref: str, note: str = "") -> bool:
        now = self.clock()
        cost_share = p["cost_usd"] * sold_raw / p["raw"] if p["raw"] else 0.0
        self.journal.record_exit(mode=self.mode, book=p["book"], position_id=p["id"], t=int(now), kind=kind, raw=sold_raw,
                                 usd=usd, cost_share=cost_share, ref=ref)
        sold_total, sold_usd = p["sold_raw"] + sold_raw, p["sold_usd"] + usd
        closed = sold_total >= p["raw"] or sold_raw == 0
        upd = {"sold_raw": sold_total, "sold_usd": sold_usd, "exit_pending": 0, "last_ref": ref}
        if not closed:
            self.journal.update_position(p["id"], **upd)
            if p["book"] == "real":
                self._feed("SELL", "CHIEF", f"${p['symbol']} · {kind} · sold {sold_raw / p['raw']:.0%} ({note})", usd - cost_share)
            return True
        pnl = sold_usd - p["cost_usd"]
        self.journal.update_position(p["id"], **upd, closed_t=int(now), pnl=pnl, exit_kind=kind)
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
    def equity(self) -> float:
        value = 0.0
        for p in self.journal.open_positions(self.mode):
            price = self.prices_now.get(p["mint"]) or p["entry_price"]
            value += (p["raw"] - p["sold_raw"]) / 10 ** p["decimals"] * price
        return self.broker.usdc() + value

    def mark(self) -> None:
        now, r = self.clock(), self.cfg.risk
        with self.lock:
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
                self.journal.put(f"start_t:{self.mode}", now)
            peak = max(float(self.journal.get(f"peak:{self.mode}") or eq), eq)
            self.journal.put(f"peak:{self.mode}", peak)
            day = day_of(now)
            if self.journal.get(f"day:{self.mode}") != day:
                self.journal.put(f"day:{self.mode}", day)
                self.journal.put(f"day_start:{self.mode}", eq)
            day_start = float(self.journal.get(f"day_start:{self.mode}") or eq)
            if self.api_errors and not self.kill.tripped and not self.journal.get(f"api_errors:{self.mode}"):
                self.errors = {k: 0 for k in self.errors}  # ./desk.sh reset cleared the streak
            if self.kill.tripped:
                return
            if peak > 0 and (peak - eq) / peak * 100 >= r.max_drawdown_pct:
                return self.trip(f"equity fell {(peak - eq) / peak:.0%} from its high (limit {r.max_drawdown_pct:g}%)")
            if day_start > 0 and (day_start - eq) / day_start * 100 >= r.daily_loss_limit_pct and self.journal.get(f"daily_stop:{self.mode}") != day:
                self.journal.put(f"daily_stop:{self.mode}", day)
                self._feed("STOP", "CHIEF", f"down {(day_start - eq) / day_start:.0%} today: no new buys until 00:00 UTC", alert=True)
            if self.mode == "live":
                try:
                    self.sol_now = self.broker.sol()
                    held = self.broker.holdings()
                except Exception as e:
                    return self._api_error(f"wallet: {e}", "wallet")
                for p in self.journal.open_positions(self.mode):
                    have, want = held.get(p["mint"], 0), p["raw"] - p["sold_raw"]
                    if have < want * 0.95:
                        self._feed("WARN", "CHIEF", f"wallet holds less ${p['symbol']} than expected: tracking {have} instead of {want}")
                        if have == 0:
                            self._finish(self.journal.position(p["id"]), 0, 0.0, "manual", "none", note="tokens left the wallet outside the bot")
                        else:
                            self.journal.update_position(p["id"], raw=p["sold_raw"] + have, cost_usd=p["cost_usd"] * (p["sold_raw"] + have) / p["raw"])

    def trip(self, reason: str) -> None:
        self.kill.trip(reason)
        self._feed("KILL", "CHIEF", f"{reason} · selling everything and stopping", alert=True)
        with self.lock:
            for p in self.journal.open_positions(self.mode):
                self._sell(p, 1.0, "kill", "kill switch")

    # ================================================================ dashboard
    def dashboard_state(self) -> dict:
        now = self.clock()
        j, mode = self.journal, self.mode
        day = day_of(now)
        start = float(j.get(f"start:{mode}") or self.equity_now or 0)
        start_t = float(j.get(f"start_t:{mode}") or now)
        curve = [(r["t"], r["v"]) for r in j.equity_curve(mode)]
        if len(curve) > 600:
            k = len(curve) / 600
            curve = [curve[int(i * k)] for i in range(600)] + [curve[-1]]
        real_closed, shadow_closed = j.closed_positions(mode, "real"), j.closed_positions(mode, "shadow")
        by_station: dict[str, dict] = {}
        for p in real_closed:
            s = by_station.setdefault(p["station"], {"n": 0, "pnl": 0.0, "wins": 0})
            s["n"] += 1
            s["pnl"] += p["pnl"]
            s["wins"] += int(p["pnl"] > 0)
        positions = []
        for p in j.open_positions(mode):
            price = self.prices_now.get(p["mint"]) or p["entry_price"]
            plan = json.loads(p["plan"])
            left = (p["raw"] - p["sold_raw"]) / p["raw"] if p["raw"] else 0
            positions.append({"symbol": p["symbol"], "mint": p["mint"], "station": p["station"], "entry": p["entry_price"], "price": price,
                              "pnlPct": round((price / p["entry_price"] - 1) * 100, 1) if p["entry_price"] else 0,
                              "cost": round(p["cost_usd"] * left, 2), "value": round((p["raw"] - p["sold_raw"]) / 10 ** p["decimals"] * price, 2),
                              "stop": plan["stop"], "rungs": plan["rungs"], "rungsDone": plan["rungs_done"], "book": plan["book"],
                              "ageMin": int((now - p["opened_t"]) / 60), "left": round(left, 3)})
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
