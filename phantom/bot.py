"""The trading loop. Models advise; this code decides, sizes, checks limits and places orders."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

from phantom.calibration import calibration_gate
from phantom.config import RiskLimits, StrategyConfig
from phantom.execution.base import Fill
from phantom.backtest import _exit_in_candle
from phantom.features import Candle, atr, build_snapshot, snapshot_hash, SCHEMA_VERSION
from phantom.journal import Journal
from phantom.risk import AccountState, KillSwitch, kill_trigger, pre_trade_vetoes
from phantom.sizing import KellyConfig, position_size
from phantom.strategy import JevView, evaluate, exit_levels, is_breakout

log = logging.getLogger(__name__)

DUST_BTC = 2e-6  # about $0.20: below this the wallet counts as holding no BTC
ALERT_REPEAT_SECONDS = 300


def view_dict(v: JevView) -> dict:
    return {"regimeUp": v.regime_up, "regimeTop": v.regime_top, "dirUp": v.dir_up, "pressure": v.pressure,
            "quality": v.quality, "extreme": v.extreme, "riskTop": v.risk_top, "tp": v.tp}


class Bot:
    def __init__(self, *, mode: str, broker, market, jev, cfg: StrategyConfig, limits: RiskLimits, journal: Journal,
                 kill: KillSwitch, alerts=None, clock=time.time):
        self.mode, self.broker, self.market, self.jev = mode, broker, market, jev
        self.cfg, self.limits, self.journal, self.kill, self.alerts, self.clock = cfg, limits, journal, kill, alerts, clock
        self.api_errors = int(journal.get(f"api_errors:{mode}", 0))
        self.last_seen = int(journal.get(f"last_candle:{mode}", 0))
        self.last_price = 0.0
        self.last_atr = 0.0
        self.latencies: deque[float] = deque(maxlen=96)
        self.last_bal: dict | None = None
        self._alerted: dict[str, float] = {}

    # ---------- persisted state ----------
    def position(self) -> dict | None:
        return self.journal.get(f"position:{self.mode}")

    def _set_position(self, p: dict | None) -> None:
        self.journal.put(f"position:{self.mode}", p)

    def _alert(self, text: str) -> None:
        log.info(text)
        key = text[:60]
        if time.monotonic() - self._alerted.get(key, -1e9) < ALERT_REPEAT_SECONDS:
            return  # same alert recently: don't spam
        self._alerted[key] = time.monotonic()
        if self.alerts:
            try:
                self.alerts.send(f"[{self.mode.upper()}] {text}")
            except Exception as e:  # alerts must never break trading
                log.warning("alert failed: %s", e)

    def _api_error(self, what: str) -> None:
        self.api_errors += 1
        self.journal.put(f"api_errors:{self.mode}", self.api_errors)
        log.warning("API error %d: %s", self.api_errors, what)
        if self.api_errors >= self.limits.max_consecutive_api_errors and not self.kill.tripped:
            self.trip(f"{self.api_errors} API errors in a row (last: {what})")

    def _api_ok(self) -> None:
        if self.api_errors:
            self.api_errors = 0
            self.journal.put(f"api_errors:{self.mode}", 0)

    def _equity(self, price: float) -> tuple[float, dict]:
        b = self.last_bal = self.broker.balances()
        return b["usd"] + b["btc"] * price, b

    def kelly_enabled(self) -> bool:
        p, y = self.journal.calibration_samples()
        ok, _ = calibration_gate(p, y, int(self.cfg.calibration_gate["min_samples"]), float(self.cfg.calibration_gate["max_ece"]))
        return ok

    # ---------- kill switch ----------
    def trip(self, reason: str) -> None:
        self.kill.trip(reason)
        self._alert(f"KILL SWITCH: {reason}. Selling any position and halting.")
        self.flatten("kill")

    def flatten(self, reason: str) -> None:
        """Sell everything the wallet holds in BTC, tracked or not."""
        pos = self.position()
        if not pos and (self.last_bal or {}).get("btc", 1.0) <= DUST_BTC:
            return
        price = self.last_price or (pos or {}).get("entry", 0.0)
        try:
            price = self.market.ticker()
        except Exception:
            pass  # selling needs Jupiter, not Coinbase: carry on with the last known price
        if self._close(pos, price, reason, sell_all=True) is None:
            self._alert("FLATTEN FAILED. Open Phantom and sell the bot wallet's cbBTC for USDC by hand.")

    # ---------- exits ----------
    def _close(self, pos: dict | None, price: float, reason: str, sell_all: bool = False) -> float | None:
        """Sell the position (never more than the wallet holds). Returns P&L, or None if the sell failed."""
        try:
            before = self.last_bal = self.broker.balances()
        except Exception as e:
            self._api_error(f"balances: {e}")
            self._alert(f"SELL FAILED ({reason}): could not read the wallet. Will retry.")
            return None
        held = before["btc"]
        qty = held if (sell_all or not pos) else min(pos["qty"], held)
        if qty <= DUST_BTC:
            fill = Fill("sell", 0.0, price, 0.0, "nothing-held")
        else:
            try:
                fill = self.broker.execute(self.broker.prepare_sell(qty, price))
                self._api_ok()
            except Exception as e:
                fill = self._reconcile(before, "sell")
                if fill is None:
                    self._api_error(f"sell: {e}")
                    self._alert(f"SELL FAILED ({reason}): {e}. Will retry.")
                    return None
        try:
            self.last_bal = self.broker.balances()
        except Exception:
            pass
        self.journal.put(f"flat_marker:{self.mode}", None)
        if not pos:
            self._alert(f"SOLD {fill.qty:.6f} untracked BTC for ${fill.usd:,.2f} ({reason}). Ref {fill.ref}")
            return 0.0
        pnl = fill.usd - pos["cost_usd"] * (fill.qty / pos["qty"] if fill.qty < pos["qty"] and fill.qty > 0 else 1.0)
        r = pnl / pos["risk_usd"] if pos["risk_usd"] else 0.0
        now = int(self.clock())
        self.journal.close_trade(pos["trade_id"], exit_t=now, exit=fill.price, reason=reason, pnl=pnl, r=r, exit_ref=fill.ref)
        if pos.get("decision_id"):
            self.journal.set_result(pos["decision_id"], pnl)
        self._set_position(None)
        self._alert(f"SOLD {fill.qty:.6f} BTC at {fill.price:,.0f} ({reason}). P&L {pnl:+,.2f} USD ({r:+.2f}R). Ref {fill.ref}")
        return pnl

    def _reconcile(self, before: dict, side: str) -> Fill | None:
        """After an error, read the wallet: if the swap landed anyway, return what really happened."""
        try:
            after = self.last_bal = self.broker.balances()
        except Exception:
            return None
        d_btc, d_usd = after["btc"] - before["btc"], after["usd"] - before["usd"]
        if side == "buy" and d_btc > DUST_BTC and d_usd < 0:
            return Fill("buy", d_btc, -d_usd / d_btc, -d_usd, "reconciled-from-wallet")
        if side == "sell" and d_btc < -DUST_BTC and d_usd > 0:
            return Fill("sell", -d_btc, d_usd / -d_btc, d_usd, "reconciled-from-wallet")
        return None

    def check_exits(self, price: float) -> str | None:
        """Called between candles with the live price: sell on stop or take-profit."""
        if price:
            self.last_price = price
        price = self.last_price
        pos = self.position()
        if self.kill.tripped:  # tripped from outside (CLI, dashboard) or an earlier sell failed: keep trying to flatten
            if pos or (self.last_bal or {}).get("btc", 0.0) > DUST_BTC:
                self.flatten("kill")
                return "kill"
            return None
        if not pos or not price:
            return None
        reason = "stop" if price <= pos["stop"] else "tp" if price >= pos["tp"] else None
        if reason and (pnl := self._close(pos, price, reason)) is not None:
            self.journal.decision(t=int(self.clock()), mode=self.mode, close=price, action=f"exit-{'sl' if reason == 'stop' else 'tp'}",
                                  note="Stop hit." if reason == "stop" else "Take-profit hit.", result=pnl, candidate=0)
        return reason

    # ---------- the candle step ----------
    def on_candle(self) -> dict | None:
        now = int(self.clock())
        try:
            candles: list[Candle] = self.market.recent_candles(200)
        except Exception as e:
            self._api_error(f"market data: {e}")
            return None
        if not candles or candles[-1].end <= self.last_seen:
            return None
        if not self.kill.tripped:  # pick up a reset done from the command line
            self.api_errors = int(self.journal.get(f"api_errors:{self.mode}", self.api_errors))
        last = candles[-1]
        price = last.close
        try:
            equity, bal = self._equity(price)
        except Exception as e:  # don't mark the candle seen: it is retried on the next tick
            self._api_error(f"balances: {e}")
            return None
        self.last_seen = last.end
        self.journal.put(f"last_candle:{self.mode}", last.end)
        self.last_price = price
        a = self.last_atr = atr(candles, self.cfg.atr_period)
        gap = any(b.start - a_.start != last.seconds for a_, b in zip(candles[-120:], candles[-119:]))

        # Deposits and withdrawals while flat are not P&L: shift the peak and day start with them.
        marker = self.journal.get(f"flat_marker:{self.mode}")
        if self.position() is None and marker is not None and abs(equity - marker) > 0.01:
            delta = equity - marker
            for k in (f"peak:{self.mode}", f"day_start:{self.mode}"):
                if self.journal.get(k) is not None:
                    self.journal.put(k, float(self.journal.get(k)) + delta)
            log.info("Wallet balance changed by %+.2f USD while flat: treated as a deposit/withdrawal", delta)

        peak = max(float(self.journal.get(f"peak:{self.mode}", equity)), equity)
        self.journal.put(f"peak:{self.mode}", peak)
        day = datetime.fromtimestamp(last.end, timezone.utc).strftime("%Y-%m-%d")
        if self.journal.get(f"day:{self.mode}") != day:
            self.journal.put(f"day:{self.mode}", day)
            self.journal.put(f"day_start:{self.mode}", equity)
        day_start = float(self.journal.get(f"day_start:{self.mode}", equity))

        row = {"t": last.end, "close": price, "action": "skip", "note": "", "result": None}
        pos = self.position()
        exited = False
        if pos:
            pos["bars"] = (last.end - pos["entry_candle"]) // last.seconds if pos.get("entry_candle") else pos["bars"] + 1
            self._set_position(pos)
            if pos["bars"] >= self.cfg.time_stop_candles:
                pnl = self._close(pos, price, "time")
                if pnl is not None:
                    row.update(action="exit-time", note=f"Time stop: {self.cfg.time_stop_candles} candles without stop or take-profit.", result=pnl)
                    exited, pos = True, None

        bal = self.last_bal or bal
        held = bal["btc"]
        if pos:
            mismatch = held + 1e-9 < pos["qty"] * 0.99 or held > pos["qty"] * 1.01 + DUST_BTC
        else:
            mismatch = held > DUST_BTC  # BTC in the wallet that the bot is not tracking
        acct = AccountState(equity=equity, peak_equity=peak, day_start_equity=day_start,
                            open_notional=pos["qty"] * price if pos else 0.0, consecutive_api_errors=self.api_errors,
                            candles_since_data=max(0, (now - last.end) // last.seconds), position_mismatch=mismatch)
        trigger = kill_trigger(acct, self.limits)
        if trigger and not self.kill.tripped:
            self.trip(trigger)
        elif self.kill.tripped and pos:
            self.flatten("kill")
        pos = self.position()

        position_info = None
        if pos:
            position_info = {"bars": pos["bars"], "open_r": round((price - pos["entry"]) * pos["qty"] / pos["risk_usd"], 3)}
        snap = build_snapshot(candles, last.end, self.cfg.breakout_lookback, self.cfg.atr_period, position=position_info)
        view: JevView | None = None
        raw, latency = None, None
        if snap and not self.kill.tripped:
            try:
                view, raw, latency = self.jev.ask(snap)
                self.latencies.append(latency)
                self._api_ok()
            except Exception as e:
                self._api_error(f"Jev: {e}")
        candidate = bool(snap) and is_breakout(snap, self.cfg)
        ev = evaluate(view, self.cfg) if view else None

        if self.kill.tripped:
            row.update(action="halt", note=f"Kill switch is on: {self.kill.reason}. No trading until a manual reset.")
        elif exited:
            pass
        elif pos:
            row.update(action="hold", note=f"In trade, {pos['bars']} candles, {position_info['open_r']:+.2f}R open.")
        elif snap is None:
            row["note"] = "Not enough candle history yet."
        elif gap:
            row["note"] = "Missing candles in the recent data; no new trades until the feed is complete."
        elif view is None:
            row["note"] = "Jev unavailable; no trade without its answer."
        elif not candidate:
            row["note"] = "No breakout: price is inside its recent range."
        elif not ev.fire:
            row["note"] = ev.failures[0][0].upper() + ev.failures[0][1:] + (f" (+{len(ev.failures) - 1} more)." if len(ev.failures) > 1 else ".")
        else:
            self._try_enter(row, price, a, equity, bal, acct, view)

        stop_lvl, tp_lvl = exit_levels(price, a, self.cfg)
        decision_id = self.journal.decision(
            t=last.end, mode=self.mode, close=price, schema=SCHEMA_VERSION, snap_hash=snapshot_hash(snap) if snap else None,
            snapshot=snap or {}, jev={"view": view_dict(view), "raw": raw} if view else {}, score=ev.score if ev else None,
            latency_ms=latency, candidate=int(candidate), action=row["action"], note=row["note"], result=row["result"],
            tp_prob=view.tp if view else None, label_due=last.end + self.cfg.time_stop_candles * last.seconds,
            stop_level=stop_lvl, tp_level=tp_lvl)
        if row["action"] == "enter":
            p = self.position()
            p["decision_id"] = decision_id
            self._set_position(p)
        self._resolve_labels(candles, now)
        try:
            eq_now = self._equity(price)[0]
            self.journal.equity(last.end, self.mode, eq_now)
            self.journal.put(f"flat_marker:{self.mode}", eq_now if self.position() is None else None)
        except Exception as e:
            self._api_error(f"balances: {e}")
        return row

    def _try_enter(self, row: dict, price: float, a: float, equity: float, bal: dict, acct: AccountState, view: JevView) -> None:
        stop, _ = exit_levels(price, a, self.cfg)
        kelly_on = self.kelly_enabled()
        size = position_size(equity=equity, entry=price, stop=stop, p=view.tp, b=self.cfg.payoff_ratio,
                             kelly=KellyConfig(**self.cfg.kelly), limits=self.limits, kelly_enabled=kelly_on,
                             fixed_risk_pct=self.cfg.fixed_risk_pct)
        usd = min(size.notional, bal["usd"])
        if size.qty <= 0 or usd < 1.0:
            row.update(action="skip", note="Size rounds to zero (probability below the Kelly cutoff, or no USDC).")
            return
        try:
            prepared = self.broker.prepare_buy(usd, price)
            self._api_ok()
        except Exception as e:
            self._api_error(f"quote: {e}")
            row.update(action="veto", note=f"Could not get a swap quote: {e}")
            return
        worst = prepared.worst_price if prepared.worst_price is not None else prepared.implied_price
        deviation = max(abs(prepared.implied_price / price - 1), abs(worst / price - 1)) * 1e4
        vetoes = pre_trade_vetoes(usd, acct, self.limits, self.kill, deviation, prepared.price_impact_bps)
        if self.mode == "live" and bal.get("sol", 0) < self.limits.min_sol_for_fees:
            vetoes.append(f"wallet has under {self.limits.min_sol_for_fees} SOL for network fees")
        if vetoes:
            row.update(action="veto", note="Risk veto: " + "; ".join(vetoes) + ".")
            return
        try:
            fill = self.broker.execute(prepared)
            self._api_ok()
        except Exception as e:
            fill = self._reconcile(bal, "buy")  # did it land anyway?
            if fill is None:
                self._api_error(f"buy: {e}")
                row.update(action="veto", note=f"Swap failed: {e}")
                return
            log.warning("Buy reported an error but the wallet shows it landed; tracking it: %s", e)
        stop, tp = exit_levels(fill.price, a, self.cfg)
        risk_usd = fill.qty * (fill.price - stop)
        trade_id = self.journal.open_trade(mode=self.mode, entry_t=int(self.clock()), entry=fill.price, qty=fill.qty, stop=stop, tp=tp,
                                           prob=view.tp, risk_usd=risk_usd, kelly_risk=size.kelly_risk_pct, entry_ref=fill.ref,
                                           strategy_version=self.cfg.version)
        self._set_position({"trade_id": trade_id, "decision_id": None, "entry": fill.price, "qty": fill.qty, "cost_usd": fill.usd,
                            "stop": stop, "tp": tp, "prob": view.tp, "risk_usd": risk_usd, "kelly_risk": size.kelly_risk_pct,
                            "bars": 0, "entry_t": int(self.clock()), "entry_candle": self.last_seen})
        self.journal.put(f"flat_marker:{self.mode}", None)
        sizing = "Kelly" if kelly_on else f"fixed {self.cfg.fixed_risk_pct:.1%} risk (Kelly off until calibration passes)"
        caps = f"; capped by {', '.join(size.capped_by)}" if size.capped_by else ""
        row.update(action="enter", note=f"Bought {fill.qty:.6f} BTC for ${fill.usd:,.2f}. Stop {stop:,.0f}, target {tp:,.0f}. Sizing: {sizing}{caps}.")
        self._alert(f"BOUGHT {fill.qty:.6f} BTC at {fill.price:,.0f} for ${fill.usd:,.2f}. Stop {stop:,.0f}, target {tp:,.0f}. "
                    f"P(TP first) {view.tp:.2f}. Ref {fill.ref}")

    def _resolve_labels(self, candles: list[Candle], now: int) -> None:
        for row in self.journal.pending_labels(now):
            future = [c for c in candles if c.start >= row["t"]][: self.cfg.time_stop_candles]
            broken = (not future or future[0].start != row["t"]
                      or any(y.start - x.start != x.seconds for x, y in zip(future, future[1:])))
            if broken:  # the candles right after this decision are missing: unusable for calibration
                self.journal.set_label(row["id"], -1)
                continue
            label = None
            for c in future:
                hit = _exit_in_candle(c, row["stop_level"], row["tp_level"])
                if hit:
                    label = 1 if hit[0] == "tp" else 0
                    break
            if label is None and len(future) >= self.cfg.time_stop_candles:
                label = 0
            if label is not None:
                self.journal.set_label(row["id"], label)

    # ---------- dashboard ----------
    def dashboard_state(self) -> dict:
        price = self.last_price
        pos = self.position()
        b = self.last_bal or {"usd": 0.0, "btc": 0.0}  # cached: never call the RPC while holding the trading lock
        equity = b["usd"] + b["btc"] * price
        if pos:
            equity -= (price - pos["entry"]) * pos["qty"]  # the page adds the open P&L back itself
        tape = []
        for r in self.journal.recent_decisions(self.mode, 60):
            jev = json.loads(r["jev"]) if r["jev"] else {}
            tape.append({"t": r["t"] * 1000, "close": r["close"], "j": jev.get("view"), "sc": r["score"], "lat": r["latency_ms"],
                         "action": r["action"], "note": r["note"], "result": r["result"]})
        closed = [{"pnl": t["pnl"], "r": t["r"], "t": t["exit_t"] * 1000} for t in self.journal.closed_trades(self.mode)]
        p, y = self.journal.calibration_samples()
        since = int(self.clock()) - 7 * 86_400
        return {
            "mode": self.mode, "t": self.last_seen * 1000, "price": price, "atr": self.last_atr, "equity": equity,
            "peak": float(self.journal.get(f"peak:{self.mode}", equity)), "dayStart": float(self.journal.get(f"day_start:{self.mode}", equity)),
            "pos": None if not pos else {"entry": pos["entry"], "stop": pos["stop"], "tp": pos["tp"], "qty": pos["qty"], "prob": pos["prob"],
                                         "bars": pos["bars"], "riskUsd": pos["risk_usd"], "kellyRisk": pos["kelly_risk"]},
            "tape": tape, "eq": [{"t": r["t"] * 1000, "v": r["v"]} for r in self.journal.equity_curve(self.mode, since)],
            "closed": closed, "calib": [{"p": a, "y": b_} for a, b_ in zip(p, y)], "lat": list(self.latencies),
            "killed": self.kill.tripped, "killReason": self.kill.reason, "apiErr": self.api_errors,
            "kellyOn": self.kelly_enabled(), "calibNeed": int(self.cfg.calibration_gate["min_samples"]),
            "limits": {"dailyLoss": self.limits.daily_loss_limit_pct, "maxDD": self.limits.max_drawdown_pct,
                       "maxPos": self.limits.max_position_pct, "maxPosUsd": self.limits.max_position_usd,
                       "apiErr": self.limits.max_consecutive_api_errors},
            "timeStopBars": self.cfg.time_stop_candles,
        }
