"""The copy-trading loop.

Every second: new trades by the trader we copy (and by anyone whose copy we still hold).
  - they buy  -> checks, size (0.25x theirs, capped), buy the same token for USDC
  - they sell -> sell the same share of our copy
Every 15 seconds: prices, stop-loss / take-profit / max-hold, equity, the hard limits.
When a copy closes, its P&L goes to the trader's record; N losses in a row fires them and the next
trader on the weekly top takes over.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from fomo.brokers import Fill, Quote
from fomo.config import FomoConfig
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


class CopyBot:
    def __init__(self, *, mode: str, cfg: FomoConfig, broker, watcher, prices, dex, mint_info: Callable,
                 lineup: Callable[[], list[Trader]], journal: Journal, kill: KillSwitch, alerts=None, clock=time.time):
        self.mode, self.cfg, self.broker, self.watcher, self.prices, self.dex = mode, cfg, broker, watcher, prices, dex
        self.mint_info, self.lineup_source, self.journal, self.kill, self.alerts, self.clock = mint_info, lineup, journal, kill, alerts, clock
        self.rot = Rotation(cfg.copy.fire_after_losses, cfg.copy.fire_cooldown_days * 86400, journal.get(f"rotation:{mode}"))
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

    # ------------------------------------------------------------ helpers
    def _save_rotation(self) -> None:
        self.journal.put(f"rotation:{self.mode}", self.rot.s)

    def _feed(self, kind: str, text: str, pnl: float | None = None, alert: bool = False) -> None:
        self.journal.event(self.mode, self.clock(), kind, text, pnl)
        log.info("[%s] %s%s", kind, text, f" ({pnl:+.2f})" if pnl is not None else "")
        if alert:
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
            self._alert("The weekly top is empty: add traders to config/traders.yaml or set FOMOAPI_KEY.")
            return
        before = self.rot.s["following"]
        for e in self.rot.set_lineup(traders[: self.cfg.copy.lineup_size], now):
            self._feed("SCAN" if "loaded" in e else "PICK", e, alert=self.rot.s["following"] != before)
        self.journal.put(f"lineup_at:{self.mode}", now)
        self._save_rotation()

    def watched_wallets(self) -> set[str]:
        w = {c["trader"] for c in self.journal.open_copies(self.mode)}
        if self.rot.s["following"]:
            w.add(self.rot.s["following"])
        return w

    # ------------------------------------------------------------ one step
    def step(self) -> None:
        self.refresh_lineup()
        try:
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
        self._api_ok("general")  # the whole step ran: a streak of loop crashes is broken

    def on_trade(self, sw) -> None:
        if sw.side == "buy":
            if sw.wallet == self.rot.s["following"]:
                self.copy_buy(sw)
            return
        if not self.cfg.copy.exit_with_trader:
            return
        for c in self.journal.open_copies(self.mode):
            if c["trader"] == sw.wallet and c["mint"] == sw.mint:
                handle = c["handle"]
                self._feed("SIG", f"@{handle} sold {sw.sold_fraction:.0%} of ${c['symbol']}")
                self.sell(c, sw.sold_fraction, "trader sold")

    # ------------------------------------------------------------ buying
    def _skip(self, sw, symbol: str, why: str) -> None:
        handle = (self.rot.following.handle if self.rot.following else sw.wallet[:6])
        text = f"@{handle} bought ${symbol} · not copied: {why}"
        if why == self._last_skip[0] and self.clock() - self._last_skip[1] < 600:
            log.info("[SKIP] %s", text)  # same reason again (daily limit, kill switch): keep the feed readable
            return
        self._last_skip = (why, self.clock())
        self._feed("SKIP", text)

    def copy_buy(self, sw) -> None:
        cfg, lim, now = self.cfg, self.cfg.limits, self.clock()
        trader = self.rot.following
        symbol = sw.mint[:4]
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
            return self._skip(sw, symbol, f"copy size ${max(usd, 0):.2f} under the ${cfg.copy.min_trade_usd:g} minimum")
        usd = round(usd, 2)

        info = self._mint(sw.mint)
        stats = self.dex.get(sw.mint)
        symbol = stats.symbol if stats else symbol
        bad = token_vetoes(info, stats, cfg.safety)
        if bad:
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
        self._feed("COPY", f"@{trader.handle if trader else '?'} bought ${symbol} · copy {cfg.copy.copy_ratio:g}x · ${fill.usd:,.2f}", alert=True)

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
        self._feed("EXIT", f"@{c['handle']} ${c['symbol']} · {reason} · we exit", pnl, alert=True)
        fired, events = self.rot.record(c["trader"], pnl, now)
        for e in events:
            self._feed("FIRE" if "fired" in e else "PICK", e, alert=True)
        self._save_rotation()
        if fired and not self.rot.s["following"]:
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
        lineup = []
        for t in self.rot.lineup:
            s = self.rot.stats(t.wallet)
            status = "following" if cur and t.wallet == cur.wallet else "fired" if self.rot.is_fired(t.wallet, now) else "bench"
            lineup.append({"rank": t.rank, "handle": t.handle, "wallet": t.wallet, "pnl7d": t.pnl_7d, "winRate": t.win_rate,
                           "status": status, "us": round(s["pnl"], 2), "trades": s["trades"]})
        positions = []
        for c in self.journal.open_copies(self.mode):
            cost_px = c["cost_usd"] / (c["raw"] / 10 ** c["decimals"])
            p = self.marks.get(c["mint"], cost_px)
            left = self.remaining(c) / c["raw"] if c["raw"] else 0
            positions.append({"symbol": c["symbol"], "mint": c["mint"], "handle": c["handle"], "cost": round(c["cost_usd"] * left, 2),
                              "value": round(self.remaining(c) / 10 ** c["decimals"] * p, 2), "pnlPct": round((p / cost_px - 1) * 100, 1),
                              "ageMin": int((now - c["opened_t"]) / 60)})
        return {
            "mode": self.mode, "now": now, "killed": self.kill.tripped, "killReason": self.kill.reason,
            "wsConnected": bool(getattr(self.watcher, "ws_connected", False)),
            "balance": round(eq, 2), "start": round(start, 2), "pnl": round(eq - start, 2),
            "pnlPct": round((eq / start - 1) * 100, 1) if start else 0.0, "multiple": round(eq / start, 2) if start else 1.0,
            "tradesCopied": self.journal.count_copies(self.mode),
            "following": ({"handle": cur.handle, "wallet": cur.wallet, "rank": cur.rank, "pnl7d": cur.pnl_7d, "winRate": cur.win_rate,
                           "copyRatio": self.cfg.copy.copy_ratio, "last6": st["last"], "withHim": round(st["pnl"], 2)} if cur else None),
            "fireRule": {"after": self.cfg.copy.fire_after_losses, "streak": st["streak"] if st else 0, "firedSoFar": self.rot.s["fires"]},
            "equity": curve, "stints": self.rot.s["stints"], "lineup": lineup, "positions": positions,
            "feed": [{"t": r["t"], "kind": r["kind"], "text": r["text"], "pnl": r["pnl"]} for r in self.journal.events(self.mode, 80)],
            "dailyStop": self.journal.get(f"daily_stop:{self.mode}") == datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
            "limits": {"maxTrade": self.cfg.copy.max_trade_usd, "maxOpen": self.cfg.limits.max_open_positions,
                       "stopLoss": self.cfg.exits.stop_loss_pct, "maxDrawdown": self.cfg.limits.max_drawdown_pct},
        }
