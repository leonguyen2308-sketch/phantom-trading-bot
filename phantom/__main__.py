"""Command line: python -m phantom <command>. Run with no command for help."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from phantom.config import file_sha256, install_redaction, load_env, load_risk, load_strategy

STATE, REPORTS = Path("state"), Path("reports")
RISK, STRATEGY = Path("config/risk.yaml"), Path("config/strategy.yaml")
log = logging.getLogger("phantom")


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler(sys.stdout), RotatingFileHandler("logs/phantom.log", maxBytes=5_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    install_redaction()


def need(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. Copy .env.example to .env and fill them in.")


# ---------------------------------------------------------------- run the bot

def cmd_run(mode: str, port: int) -> None:
    from phantom.bot import Bot
    from phantom.calibration import brier, fit_isotonic
    from phantom.execution.paper import PaperBroker
    from phantom.golive import go_live_report
    from phantom.jev import TypeSafeJev
    from phantom.journal import Journal
    from phantom.market import CoinbaseMarket
    from phantom.risk import KillSwitch
    from phantom.server import make_app, serve_in_background

    cfg, limits = load_strategy(STRATEGY), load_risk(RISK)
    risk_hash = file_sha256(RISK)
    approved = (STATE / "risk_approved.sha256")
    if not approved.exists() or approved.read_text().strip() != risk_hash:
        if mode == "live":
            sys.exit("config/risk.yaml has not been approved. Run: python -m phantom approve-limits")
        log.warning("Hard limits are not approved yet (python -m phantom approve-limits). Paper trading anyway.")

    journal, kill = Journal(STATE / "journal.sqlite"), KillSwitch(STATE / "kill.json")
    need("TYPESAFE_API_KEY")

    recal = None
    p, y = journal.calibration_samples()
    if len(p) >= int(cfg.calibration_gate["min_samples"]):
        cut = int(len(p) * 0.7)
        f = fit_isotonic(p[:cut], y[:cut])
        if brier([f(q) for q in p[cut:]], y[cut:]) < brier(p[cut:], y[cut:]):
            recal = fit_isotonic(p, y)
            log.info("Recalibrating Jev's TP-before-stop probability (isotonic, %d samples).", len(p))
    jev = TypeSafeJev(cfg, cache_path=STATE / "jev_cache.sqlite", model=os.environ.get("JEV_MODEL") or None, recalibrate_tp=recal)

    if mode == "live":
        report = go_live_report(STATE, REPORTS, RISK, STRATEGY)
        print(report.text())
        open_live = journal.get("position:live") is not None
        if not report.ok or os.environ.get("LIVE_TRADING") != "yes":
            if not open_live:
                sys.exit(1 if not report.ok else "Go-live checks pass, but LIVE_TRADING=yes is not set in .env. Set it to trade real money.")
            # Never abandon an open position: start halted and sell it.
            kill.trip("Live checks no longer pass while a live position is open; selling it")
            log.warning("Live checks fail with an open live position: starting halted to sell it.")
        need("PHANTOM_BOT_PRIVATE_KEY", "PHANTOM_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL")
        from phantom.execution.jupiter import JupiterBroker
        from phantom.wallet import load_keypair
        kp = load_keypair(os.environ["PHANTOM_BOT_PRIVATE_KEY"], os.environ["PHANTOM_BOT_ADDRESS"])
        broker = JupiterBroker(kp, os.environ["JUPITER_API_KEY"], os.environ["SOLANA_RPC_URL"])
        broker.verify_mints()
        log.info("LIVE trading from bot wallet %s", kp.pubkey())
    else:
        broker = PaperBroker(STATE / "paper.json", start_usd=float(os.environ.get("PAPER_START_USD", "1000")),
                             fee_bps=float(os.environ.get("PAPER_FEE_BPS", "10")), slippage_bps=float(os.environ.get("PAPER_SLIPPAGE_BPS", "5")))

    alerts = None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from phantom.alerts import Telegram
        alerts = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

    market = CoinbaseMarket()
    bot = Bot(mode=mode, broker=broker, market=market, jev=jev, cfg=cfg, limits=limits, journal=journal, kill=kill, alerts=alerts)
    lock = threading.Lock()

    def trip(reason: str) -> None:
        with lock:
            if not bot.kill.tripped:
                bot.trip(reason)

    serve_in_background(make_app(bot.dashboard_state, lambda r: bot.trip(r), lock), port)
    log.info("Dashboard: http://localhost:%d", port)
    if alerts:
        def status() -> str:
            with lock:
                s = bot.dashboard_state()
            pos = s["pos"]
            return (f"{mode.upper()} | price {s['price']:,.0f} | equity ${s['equity'] + ((s['price'] - pos['entry']) * pos['qty'] if pos else 0):,.2f} | "
                    f"{'LONG ' + format(pos['qty'], '.6f') + ' BTC' if pos else 'flat'} | kill switch {'ON: ' + s['killReason'] if s['killed'] else 'armed'}")
        alerts.listen({"/status": status, "/kill": lambda: (trip("Telegram /kill"), "Kill switch fired. Position sold; trading halted.")[1]})
        bot._alert(f"Bot started in {mode.upper()} mode. Send /status or /kill.")

    ticker_errors = 0
    with lock:
        try:
            bot._equity(bot.last_price or market.ticker())  # read the wallet once so the dashboard has numbers
        except Exception as e:
            log.warning("Startup wallet read failed: %s", e)
    while True:  # every 15 s: limits file, live price -> stops/take-profit/kill, then any newly closed candle
        try:
            changed = not RISK.exists() or file_sha256(RISK) != risk_hash
        except OSError:
            changed = True
        if changed:
            trip("config/risk.yaml changed or disappeared while running")
        price = None
        try:
            price = market.ticker()
            ticker_errors = 0
        except Exception as e:
            ticker_errors += 1
            log.warning("Price check failed (%d in a row): %s", ticker_errors, type(e).__name__)
            if ticker_errors >= limits.max_consecutive_api_errors:
                trip(f"No live price for {ticker_errors} checks in a row")
        try:
            with lock:
                bot.check_exits(price)
                row = bot.on_candle()  # returns None until a new candle has closed and been published
            if row:
                log.info("%s %s: %s", datetime.fromtimestamp(row["t"], timezone.utc).strftime("%H:%M"), row["action"], row["note"])
        except Exception as e:  # never die with a position open: log, count, keep watching
            log.exception("Loop error")
            with lock:
                bot._api_error(f"loop: {type(e).__name__}: {e}")
        time.sleep(15)


# ---------------------------------------------------------------- backtest

def cmd_backtest(days: int, use_jev: bool, holdout: bool, fee_bps: float, slippage_bps: float, start_usd: float) -> None:
    from phantom.backtest import (Costs, deflated_sharpe_prob, gate, metrics, regime_breakdown, run_backtest, split_periods)
    from phantom.market import CoinbaseMarket

    cfg, limits = load_strategy(STRATEGY), load_risk(RISK)
    print(f"Downloading {days} days of BTC-USD 15m candles from Coinbase (cached in data/)...")
    candles = CoinbaseMarket().history(days, progress=lambda f: print(f"\r  {f:5.0%}", end="", flush=True))
    print(f"\r  {len(candles)} candles.")
    dev, oos, hold = split_periods(candles)
    notes = []
    jev = None
    if use_jev:
        need("TYPESAFE_API_KEY")
        from phantom.jev import TypeSafeJev
        tj = TypeSafeJev(cfg, cache_path=STATE / "jev_cache.sqlite", model=os.environ.get("JEV_MODEL") or None)
        jev = lambda snap: tj.ask(snap)[0]  # noqa: E731
        if not cfg.jev_training_cutoff:
            notes.append("jev_training_cutoff is not set in strategy.yaml, so Jev results cannot count as out of sample.")
        else:
            cutoff = int(datetime.fromisoformat(str(cfg.jev_training_cutoff)).replace(tzinfo=timezone.utc).timestamp())
            if oos[0].start < cutoff:
                notes.append("Part of the out-of-sample window is before Jev's training cutoff; it may have seen that data.")
    costs = Costs(fee_bps, slippage_bps)
    res = {name: run_backtest(cs, cfg, limits, jev, costs, start_usd) for name, cs in (("dev", dev), ("oos", oos))}
    res_2x = run_backtest(oos, cfg, limits, jev, costs.doubled(), start_usd)
    m = {k: metrics(r.equity, r.trades) for k, r in res.items()}
    m2 = metrics(res_2x.equity, res_2x.trades)

    trials_path = STATE / "trials.json"
    trials = json.loads(trials_path.read_text()) if trials_path.exists() else []
    trials.append({"at": int(time.time()), "version": cfg.version, "jev": use_jev, "oos_sharpe": m["oos"]["sharpe"]})
    STATE.mkdir(exist_ok=True)
    trials_path.write_text(json.dumps(trials, indent=1))
    dsr = deflated_sharpe_prob(m["oos"], [t["oos_sharpe"] for t in trials if t["jev"] == use_jev])
    regimes = regime_breakdown(res["oos"].trades)
    checks = gate(m["oos"], m2, dsr, regimes, cfg.gate, killed=res["oos"].killed)
    oos_ok = all(c["ok"] for c in checks.values()) and not notes

    hold_m = None
    if holdout:
        with open(STATE / "holdout_access.log", "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} version={cfg.version} jev={use_jev}\n")
        hr = run_backtest(hold, cfg, limits, jev, costs, start_usd)
        hold_m = metrics(hr.equity, hr.trades)
    hold_ok = bool(hold_m and hold_m["sharpe"] > 1.0 and hold_m["max_drawdown"] < cfg.gate["max_drawdown_max"])
    passed = oos_ok and hold_ok

    REPORTS.mkdir(exist_ok=True)
    holdout_runs = sum(1 for line in open(STATE / "holdout_access.log") if f"version={cfg.version} " in line) if (STATE / "holdout_access.log").exists() else 0
    report = {"strategy_version": cfg.version, "jev": use_jev, "passed": passed, "holdout_runs": holdout_runs, "jev_cutoff": str(cfg.jev_training_cutoff), "costs": asdict(costs), "trials": len(trials),
              "dev": m["dev"], "oos": m["oos"], "oos_2x_costs": m2, "deflated_sharpe_prob": dsr, "checks": checks,
              "regimes": regimes, "holdout": hold_m, "notes": notes, "killed_in_oos": res["oos"].killed,
              "periods": {k: [datetime.fromtimestamp(cs[0].start, timezone.utc).date().isoformat(),
                              datetime.fromtimestamp(cs[-1].end, timezone.utc).date().isoformat()] for k, cs in (("dev", dev), ("oos", oos), ("holdout", hold))}}
    (REPORTS / "gate.json").write_text(json.dumps(report, indent=1, default=str))

    print(f"\nStrategy {cfg.version}  |  Jev: {'on' if use_jev else 'off (rules only)'}  |  trial #{len(trials)}")
    for k in ("dev", "oos"):
        x = m[k]
        print(f"  {k.upper():4} Sharpe {x['sharpe']:5.2f}  maxDD {x['max_drawdown']:6.1%}  hit {x['hit_rate']:5.1%}  t {x['t_stat']:5.2f}  "
              f"trades {x['trades']:4d}  return {x['total_return']:+7.1%}")
    print("\nOut-of-sample gate:")
    for name, c in checks.items():
        v = c["value"]
        vs = f"{v:.3f}" if isinstance(v, float) else str(v)
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {name:16} {vs:>24}  (rule {c['rule']})")
    for n in notes:
        print(f"  [FAIL] {n}")
    if hold_m:
        print(f"\nHoldout: Sharpe {hold_m['sharpe']:.2f}, maxDD {hold_m['max_drawdown']:.1%}, trades {hold_m['trades']}  ->  {'PASS' if hold_ok else 'FAIL'}")
    elif oos_ok:
        print("\nOut of sample passes. Run once more with --holdout to grade the locked final 90 days.")
    print(f"\nGATE: {'PASSED' if passed else 'NOT PASSED'}   (report: reports/gate.json)")


# ---------------------------------------------------------------- safety drills and approvals

def cmd_test_kill() -> None:
    """Fire the kill switch through every trigger, on paper, and record the evidence."""
    from phantom.bot import Bot
    from phantom.execution.paper import PaperBroker
    from phantom.journal import Journal
    from phantom.risk import KillSwitch
    from phantom.sim import FakeJev, FakeMarket
    from phantom.strategy import JevView

    cfg, limits = load_strategy(STRATEGY), load_risk(RISK)
    yes = JevView(0.8, "trending_up", 0.8, 0.8, 3.5, 0.02, "normal", 0.7)
    results = {}

    def drill(name, trigger):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            market = FakeMarket()
            bot = Bot(mode="paper", broker=PaperBroker(d / "p.json", start_usd=1000), market=market, jev=FakeJev(yes), cfg=cfg,
                      limits=limits, journal=Journal(d / "j.sqlite"), kill=KillSwitch(d / "k.json"), clock=market.clock)
            entered = (bot.on_candle() or {}).get("action") == "enter"
            trigger(bot, market)
            ok = entered and bot.kill.tripped and bot.position() is None and bot.broker.balances()["btc"] < 1e-9
            results[name] = {"ok": ok, "reason": bot.kill.reason, "at": int(time.time())}
            print(f"  [{'PASS' if ok else 'FAIL'}] {name:18} {bot.kill.reason}")

    def drawdown(bot, market):
        bot.journal.put("peak:paper", 10_000.0)
        market.advance()
        bot.on_candle()

    def api_errors(bot, market):
        bot.jev.fail = True
        for _ in range(limits.max_consecutive_api_errors + 1):
            market.advance()
            bot.on_candle()

    def mismatch(bot, market):
        bot.broker.bal["btc"] *= 0.5
        market.advance()
        bot.on_candle()

    print("Kill switch drill (paper, simulated market):")
    drill("drawdown", drawdown)
    drill("api_errors", api_errors)
    drill("position_mismatch", mismatch)
    drill("manual", lambda bot, market: bot.trip("manual /kill drill"))
    STATE.mkdir(exist_ok=True)
    results["_meta"] = {"at": int(time.time()), "risk_sha256": file_sha256(RISK)}
    (STATE / "kill_tests.json").write_text(json.dumps(results, indent=1))
    print("All paths fired." if all(r["ok"] for k, r in results.items() if k != "_meta") else "Some paths FAILED. Do not go live.")


def cmd_approve_limits() -> None:
    print(RISK.read_text())
    if input("Type APPROVE to lock in these hard limits: ").strip() != "APPROVE":
        sys.exit("Not approved.")
    STATE.mkdir(exist_ok=True)
    (STATE / "risk_approved.sha256").write_text(file_sha256(RISK))
    print("Approved. The bot refuses to trade if this file changes.")


def cmd_ack_risks() -> None:
    from phantom.golive import BLOW_UP_RISKS
    print("WHAT COULD BLOW UP THIS ACCOUNT?\n")
    for i, r in enumerate(BLOW_UP_RISKS, 1):
        print(f"{i}. {r}")
        if input("   Type I ACCEPT: ").strip() != "I ACCEPT":
            sys.exit("Stopped. Live trading stays off.")
    STATE.mkdir(exist_ok=True)
    (STATE / "ack_risks.json").write_text(json.dumps({"count": len(BLOW_UP_RISKS), "at": int(time.time())}))
    print("Recorded.")


def cmd_golive_check() -> None:
    from phantom.golive import go_live_report
    report = go_live_report(STATE, REPORTS, RISK, STRATEGY)
    print(report.text())
    sys.exit(0 if report.ok else 1)


def cmd_kill(reset: bool) -> None:
    from phantom.risk import KillSwitch
    ks = KillSwitch(STATE / "kill.json")
    if not reset:
        ks.trip("Command line: python -m phantom kill")
        print("Kill switch ON. The running bot sells any position and halts at its next check (within 15 seconds).")
        return
    print(f"Kill switch is {'ON: ' + ks.reason if ks.tripped else 'off'}.")
    ks.reset(input("Type RESET to allow trading again: ").strip())
    from phantom.journal import Journal
    j = Journal(STATE / "journal.sqlite")
    for m in ("paper", "live"):  # start the error count fresh, or the old count re-trips it at once
        j.put(f"api_errors:{m}", 0)
    print("Kill switch reset. Restart the bot (or it picks this up within 15 seconds).")


def cmd_jev_test() -> None:
    need("TYPESAFE_API_KEY")
    from phantom.jev import TypeSafeJev
    from phantom.sim import FakeMarket
    from phantom.features import build_snapshot
    cfg = load_strategy(STRATEGY)
    m = FakeMarket()
    snap = build_snapshot(m.recent_candles(), m.recent_candles()[-1].end)
    view, raw, ms = TypeSafeJev(cfg, cache_path=STATE / "jev_cache.sqlite").ask(snap, use_cache=False)
    print(f"Jev answered in {ms:.0f} ms:\n  {view}")


def cmd_wallet_check() -> None:
    need("PHANTOM_BOT_PRIVATE_KEY", "PHANTOM_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL")
    from phantom.execution.jupiter import JupiterBroker
    from phantom.wallet import load_keypair
    kp = load_keypair(os.environ["PHANTOM_BOT_PRIVATE_KEY"], os.environ["PHANTOM_BOT_ADDRESS"])
    b = JupiterBroker(kp, os.environ["JUPITER_API_KEY"], os.environ["SOLANA_RPC_URL"])
    b.verify_mints()
    bal = b.balances()
    print(f"Bot wallet {kp.pubkey()}\n  USDC {bal['usd']:,.2f}\n  cbBTC {bal['btc']:.8f}\n  SOL {bal['sol']:.4f}")
    q = b.prepare_buy(10.0, 0)
    print(f"  Quote check: $10 USDC -> {q.expected_out:.8f} cbBTC (implied {q.implied_price:,.0f} USD/BTC, impact {q.price_impact_bps:.1f} bps). Nothing was sent.")


def main() -> None:
    load_env(".env")
    ap = argparse.ArgumentParser(prog="python -m phantom", description="Phantom trading bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for m in ("paper", "live"):
        sp = sub.add_parser(m, help=f"run the bot in {m} mode")
        sp.add_argument("--port", type=int, default=8000)
    bt = sub.add_parser("backtest", help="download history, backtest, and grade against the gate")
    bt.add_argument("--days", type=int, default=730)
    bt.add_argument("--jev", action="store_true", help="filter with Jev (uses your TypeSafe key; answers are cached)")
    bt.add_argument("--holdout", action="store_true", help="also grade the locked final 90 days (logged)")
    bt.add_argument("--fee-bps", type=float, default=10.0)
    bt.add_argument("--slippage-bps", type=float, default=5.0)
    bt.add_argument("--start-usd", type=float, default=1000.0)
    sub.add_parser("test-kill", help="fire the kill switch through every trigger on paper and record it")
    sub.add_parser("approve-limits", help="review and lock config/risk.yaml")
    sub.add_parser("ack-risks", help="read and accept what could blow up the account")
    sub.add_parser("golive-check", help="the final check before real money")
    sub.add_parser("kill", help="fire the kill switch now")
    sub.add_parser("reset", help="reset the kill switch (asks for confirmation)")
    sub.add_parser("jev-test", help="make one real Jev call to check your key")
    sub.add_parser("wallet-check", help="check the bot wallet, balances and a Jupiter quote (sends nothing)")
    a = ap.parse_args()
    setup_logging()
    if a.cmd in ("paper", "live"):
        cmd_run(a.cmd, a.port)
    elif a.cmd == "backtest":
        cmd_backtest(a.days, a.jev, a.holdout, a.fee_bps, a.slippage_bps, a.start_usd)
    elif a.cmd == "test-kill":
        cmd_test_kill()
    elif a.cmd == "approve-limits":
        cmd_approve_limits()
    elif a.cmd == "ack-risks":
        cmd_ack_risks()
    elif a.cmd == "golive-check":
        cmd_golive_check()
    elif a.cmd in ("kill", "reset"):
        cmd_kill(reset=a.cmd == "reset")
    elif a.cmd == "jev-test":
        cmd_jev_test()
    elif a.cmd == "wallet-check":
        cmd_wallet_check()


if __name__ == "__main__":
    main()
