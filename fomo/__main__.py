"""Command line for the FOMO copy bot: python -m fomo <command> (or ./fomo.sh <command>)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from phantom.config import file_sha256, install_redaction, load_env

STATE = Path("state")
CONFIG = Path("config/fomo.yaml")
TRADERS = Path("config/traders.yaml")
WALLETS = Path("config/wallets.txt")
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
log = logging.getLogger("fomo")


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler(sys.stdout), RotatingFileHandler("logs/fomo.log", maxBytes=5_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    install_redaction()


def need(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. See FOMO_BOT.md, step 2.")


def load_bot_keypair():
    from solders.keypair import Keypair
    try:
        kp = Keypair.from_base58_string(os.environ["FOMO_BOT_PRIVATE_KEY"].strip())
    except Exception:
        sys.exit("FOMO_BOT_PRIVATE_KEY is not a valid Solana private key (Phantom: Settings -> wallet -> Show Private Key).")
    if str(kp.pubkey()) != os.environ["FOMO_BOT_ADDRESS"].strip():
        sys.exit(f"FOMO_BOT_PRIVATE_KEY belongs to {kp.pubkey()}, not FOMO_BOT_ADDRESS. Fix .env.")
    return kp


def lineup_fn(cfg):
    from fomo.leaderboard import load_lineup
    return lambda: load_lineup(cfg.copy.lineup_size, os.environ.get("FOMOAPI_KEY", ""), TRADERS,
                               tracker_key=os.environ.get("SOLANATRACKER_API_KEY", ""), wallets_path=WALLETS)


# ---------------------------------------------------------------- run

def cmd_run(mode: str, port: int, speed: float = 60.0, seed: int | None = None, traders: int | None = None) -> None:
    from fomo.bot import CopyBot
    from fomo.config import load_config
    from fomo.journal import Journal
    from phantom.risk import KillSwitch
    from phantom.server import make_app, serve_in_background

    cfg = load_config(CONFIG)
    cfg_hash = file_sha256(CONFIG)
    STATE.mkdir(exist_ok=True)
    tick = 1.0
    if mode == "demo":
        from fomo.sim import SimBroker, SimDex, SimPrices, SimWatcher, SimWorld
        for f in (STATE / "fomo_demo.sqlite", STATE / "fomo_demo_kill.json"):
            f.unlink(missing_ok=True)  # every demo starts fresh
        world = SimWorld(seed=seed, speed=speed, n_traders=traders or (40 if cfg.copy.follow_all else 6))
        dex = SimDex(world)
        journal, kill = Journal(STATE / "fomo_demo.sqlite"), KillSwitch(STATE / "fomo_demo_kill.json")
        bot = CopyBot(mode="demo", cfg=cfg, broker=SimBroker(world, 100.0, cfg.paper.slippage_bps), watcher=SimWatcher(world),
                      prices=SimPrices(world), dex=dex, mint_info=dex.mint_info, lineup=world.lineup, journal=journal, kill=kill,
                      clock=world.clock)
        tick = min(0.25, 10 / speed)  # at most 10 simulated seconds per step, so copies aren't "seen late"
        print(f"SIMULATION at {speed:g}x speed: made-up traders, made-up coins, fake money. Nothing is real.")
    else:
        need("SOLANA_RPC_URL", "JUPITER_API_KEY")
        from fomo.brokers import LiveBroker, PaperBroker
        from fomo.chain import SolanaRpc, ws_url
        from fomo.market import DexStats, Prices
        from fomo.watcher import LeaderWatcher
        rpc = SolanaRpc(os.environ["SOLANA_RPC_URL"])
        prices = Prices(os.environ["JUPITER_API_KEY"])
        journal, kill = Journal(STATE / "fomo.sqlite"), KillSwitch(STATE / "fomo_kill.json")
        if mode == "live":
            from fomo.golive import go_live_report
            approved = STATE / "fomo_approved.sha256"
            if not approved.exists() or approved.read_text().strip() != cfg_hash:
                sys.exit("config/fomo.yaml is not approved (or changed since). Run: ./fomo.sh approve")
            report = go_live_report(STATE, CONFIG)
            print(report.text())
            if not report.ok:
                if not journal.open_copies("live"):
                    sys.exit(1)
                kill.trip("Go-live checks no longer pass while live copies are open; selling them")
                log.warning("Go-live checks fail with open live copies: starting halted to sell them.")
            kp = load_bot_keypair()
            broker = LiveBroker(kp, os.environ["JUPITER_API_KEY"], rpc)
            log.info("LIVE copy trading from bot wallet %s: USDC %.2f, SOL %.4f", kp.pubkey(), broker.usdc(), broker.sol())
        else:
            start = float(os.environ.get("FOMO_PAPER_START_USD", "100"))
            broker = PaperBroker(STATE / "fomo_paper.json", os.environ["JUPITER_API_KEY"], start, cfg.paper.slippage_bps)
        crowd = cfg.copy.follow_all
        watcher = LeaderWatcher(rpc, prices.sol_usd, os.environ.get("SOLANA_WS_URL") or ws_url(os.environ["SOLANA_RPC_URL"]),
                                max_age_seconds=max(cfg.copy.max_signal_age_seconds * 2, 120),
                                ws_connections=cfg.watch.ws_connections if crowd else 1,
                                wallets_per_connection=cfg.watch.wallets_per_connection if crowd else 10 ** 9,
                                usdc_accounts=not crowd, max_wallet_txs_per_hour=cfg.watch.max_wallet_txs_per_hour if crowd else 0,
                                max_reads_per_hour=cfg.watch.max_tx_reads_per_hour if crowd else 0, max_reads_per_poll=6 if crowd else 0)
        bot = CopyBot(mode=mode, cfg=cfg, broker=broker, watcher=watcher, prices=prices, dex=DexStats(), mint_info=rpc.mint_info,
                      lineup=lineup_fn(cfg), journal=journal, kill=kill)

    alerts = None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") and mode != "demo":
        from phantom.alerts import Telegram
        alerts = bot.alerts = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

    lock = threading.Lock()

    def trip(reason: str) -> None:
        with lock:
            if not bot.kill.tripped:
                bot.trip(reason)

    page = DASHBOARD / ("crowd.html" if cfg.copy.follow_all else "fomo.html")
    serve_in_background(make_app(bot.dashboard_state, lambda r: bot.trip(r), lock, page=page), port)
    log.info("Dashboard: http://localhost:%d", port)
    if alerts:
        def status() -> str:
            with lock:
                s = bot.dashboard_state()
            f, c = s["following"], s["crowd"]
            who = f"{c['followed']} traders" if c["on"] else ('@' + f['handle']) if f else 'nobody'
            return (f"{mode.upper()} | balance ${s['balance']:,.2f} ({s['pnl']:+,.2f}) | copying {who} | "
                    f"{len(s['positions'])} open | kill switch {'ON: ' + s['killReason'] if s['killed'] else 'armed'}")
        alerts.listen({"/status": status, "/kill": lambda: (trip("Telegram /kill"), "Kill switch fired. Selling everything; copying stopped.")[1]})
        bot._alert(f"FOMO copy bot started in {mode.upper()} mode. Send /status or /kill.")

    last_cfg_check = 0.0
    while True:
        if mode == "live" and time.monotonic() - last_cfg_check > 15:
            last_cfg_check = time.monotonic()
            try:
                changed = file_sha256(CONFIG) != cfg_hash
            except OSError:
                changed = True
            if changed:
                trip("config/fomo.yaml changed while running live")
        try:
            with lock:
                bot.step()
        except Exception as e:  # never die with copies open: log, count, keep going
            log.exception("Loop error")
            with lock:
                bot._api_error(f"loop: {type(e).__name__}: {e}")
        time.sleep(tick)


# ---------------------------------------------------------------- checks and tools

def cmd_traders(check: int = 8) -> None:
    from datetime import datetime, timezone

    from fomo.config import load_config
    cfg = load_config(CONFIG)
    traders = lineup_fn(cfg)()
    if not traders:
        sys.exit("No traders. Set SOLANATRACKER_API_KEY or FOMOAPI_KEY in .env, or put wallets in config/wallets.txt (see FOMO_BOT.md, step 3).")
    print(f"Top {len(traders)} traders ({traders[0].source}); the bot copies {'all of them at once' if cfg.copy.follow_all else 'one at a time'}:")
    for t in traders[:15]:
        print(f"  #{t.rank} @{t.handle:18} {t.wallet}" + (f"  7d {t.pnl_7d:+,.0f}" if t.pnl_7d is not None else ""))
    if len(traders) > 15:
        Path("reports").mkdir(exist_ok=True)
        Path("reports/fomo_top.txt").write_text("\n".join(f"{t.rank}\t{t.handle}\t{t.wallet}" for t in traders) + "\n")
        print(f"  ... and {len(traders) - 15} more (all of them in reports/fomo_top.txt)")
    traders = traders[:check]
    if not os.environ.get("SOLANA_RPC_URL"):
        print("\nSet SOLANA_RPC_URL in .env to also check that the bot can see each trader's trades.")
        return
    from fomo.chain import SolanaRpc
    from fomo.research import SwapCache, fetch_swaps
    rpc = SolanaRpc(os.environ["SOLANA_RPC_URL"])
    sol = 150.0
    if os.environ.get("JUPITER_API_KEY"):
        from fomo.market import Prices
        try:
            sol = Prices(os.environ["JUPITER_API_KEY"]).sol_usd()
        except Exception:
            pass
    print(f"\nCan the bot see their trades? (reads the last 30 transactions of the first {len(traders)}; "
          "a check of all of them would use thousands of RPC credits)")
    cache = SwapCache(STATE / "fomo_tx_cache.sqlite")
    for t in traders:
        try:
            seen = fetch_swaps(rpc, t.wallet, time.time() - 30 * 86400, sol, 30, cache)
        except Exception as e:
            print(f"  @{t.handle:18} could not check: {e}")
            continue
        if not seen:
            print(f"  @{t.handle:18} NO trades found in the last 30 days on Solana. Wrong wallet, inactive, or trading on another chain.")
            continue
        last = seen[-1]
        ago = (time.time() - last.t) / 3600
        when = datetime.fromtimestamp(last.t, timezone.utc).strftime("%m-%d %H:%M UTC")
        print(f"  @{t.handle:18} {len(seen):2d} trades found, last {last.side} ${last.usd:,.0f} at {when} ({ago:.0f}h ago)  OK")


def cmd_wallet_check() -> None:
    need("FOMO_BOT_PRIVATE_KEY", "FOMO_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL")
    from fomo.brokers import LiveBroker
    from fomo.chain import SolanaRpc
    from fomo.config import load_config
    from fomo.swaps import WSOL
    cfg = load_config(CONFIG)
    kp = load_bot_keypair()
    b = LiveBroker(kp, os.environ["JUPITER_API_KEY"], SolanaRpc(os.environ["SOLANA_RPC_URL"]))
    usdc, sol = b.usdc(), b.sol()
    print(f"Bot wallet {kp.pubkey()}\n  USDC {usdc:,.2f}\n  SOL  {sol:.4f}")
    if sol < 0.1:
        print("  ! Keep about 0.1 SOL here: every new coin locks ~0.002 SOL in a token account, and below "
              f"{cfg.limits.min_sol_for_fees} SOL the bot stops copying.")
    if usdc < cfg.copy.min_trade_usd:
        print("  ! Send USDC (Solana) to this wallet: the bot buys with USDC.")
    q = b.quote_buy(WSOL, 5.0)
    print(f"  Quote check: $5 USDC -> {q.raw / 1e9:.5f} SOL (impact {q.impact_pct:.2f}%). Nothing was sent.")


def cmd_approve() -> None:
    from fomo.config import load_config
    load_config(CONFIG)  # refuse to approve a file the bot can't read
    print(CONFIG.read_text())
    if input("Type APPROVE to lock in these rules and limits: ").strip() != "APPROVE":
        sys.exit("Not approved.")
    STATE.mkdir(exist_ok=True)
    (STATE / "fomo_approved.sha256").write_text(file_sha256(CONFIG))
    print("Approved. Live trading refuses to start, and stops, if this file changes.")


def cmd_ack_risks() -> None:
    from fomo.golive import BLOW_UP_RISKS
    print("WHAT COULD BLOW UP THIS ACCOUNT?\n")
    for i, r in enumerate(BLOW_UP_RISKS, 1):
        print(f"{i}. {r}")
        if input("   Type I ACCEPT: ").strip() != "I ACCEPT":
            sys.exit("Stopped. Live trading stays off.")
    STATE.mkdir(exist_ok=True)
    (STATE / "fomo_ack_risks.json").write_text(json.dumps({"count": len(BLOW_UP_RISKS), "at": int(time.time())}))
    print("Recorded.")


def cmd_golive_check() -> None:
    from fomo.golive import go_live_report
    report = go_live_report(STATE, CONFIG)
    print(report.text())
    sys.exit(0 if report.ok else 1)


def cmd_kill(reset: bool) -> None:
    from fomo.journal import Journal
    from phantom.risk import KillSwitch
    ks = KillSwitch(STATE / "fomo_kill.json")
    if not reset:
        ks.trip("Command line: ./fomo.sh kill")
        print("Kill switch ON. The running bot sells every copy and stops copying at its next check (within 15 seconds).")
        return
    print(f"Kill switch is {'ON: ' + ks.reason if ks.tripped else 'off'}.")
    ks.reset(input("Type RESET to allow trading again: ").strip())
    j = Journal(STATE / "fomo.sqlite")
    for m in ("paper", "live"):  # fresh error count and high-water mark, or the old ones re-trip it at once
        j.put(f"api_errors:{m}", 0)
        j.put(f"peak:{m}", None)
    print("Kill switch reset. Restart the bot (or it picks this up within 15 seconds).")


def cmd_research(days: float, max_tx: int, top: int = 10) -> None:
    need("SOLANA_RPC_URL", "JUPITER_API_KEY")
    from fomo.chain import SolanaRpc
    from fomo.config import load_config
    from fomo.market import Prices
    from fomo.research import run_research
    cfg = load_config(CONFIG)
    traders = lineup_fn(cfg)()[:top]
    if not traders:
        sys.exit("No traders. Set SOLANATRACKER_API_KEY or FOMOAPI_KEY in .env, or put wallets in config/wallets.txt.")
    sol = Prices(os.environ["JUPITER_API_KEY"]).sol_usd()
    run_research(traders, SolanaRpc(os.environ["SOLANA_RPC_URL"]), cfg, days=days, sol_usd=sol, max_tx=max_tx,
                 cache=STATE / "fomo_tx_cache.sqlite", out=Path("reports"))


def main() -> None:
    load_env(".env")
    ap = argparse.ArgumentParser(prog="./fomo.sh", description="FOMO copy-trading bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="simulation: made-up traders and coins, fake money, no keys needed")
    d.add_argument("--speed", type=float, default=60.0, help="simulated seconds per real second (default 60)")
    d.add_argument("--seed", type=int, default=None)
    d.add_argument("--traders", type=int, default=None, help="how many made-up traders to follow (default 40, or 6 for the one-at-a-time rule)")
    d.add_argument("--port", type=int, default=8001)
    for m in ("paper", "live"):
        sp = sub.add_parser(m, help=f"copy the weekly top for {'fake' if m == 'paper' else 'REAL'} money")
        sp.add_argument("--port", type=int, default=8001)
    r = sub.add_parser("research", help="replay the copy rule on the traders' real trades from the last days")
    r.add_argument("--days", type=float, default=7.0)
    r.add_argument("--max-tx", type=int, default=600, help="most transactions to read per trader")
    r.add_argument("--top", type=int, default=10, help="research only the first N traders of the list (each one costs hundreds of RPC calls)")
    tr = sub.add_parser("traders", help="show the top list and check the first wallets on-chain")
    tr.add_argument("--check", type=int, default=8, help="how many of the first wallets to check on-chain (default 8)")
    sub.add_parser("wallet-check", help="check the bot wallet and a Jupiter quote (sends nothing)")
    sub.add_parser("approve", help="review and lock config/fomo.yaml")
    sub.add_parser("ack-risks", help="read and accept what could blow up the account")
    sub.add_parser("golive-check", help="every check before real money")
    sub.add_parser("kill", help="sell every copy and stop copying")
    sub.add_parser("reset", help="reset the kill switch (asks for confirmation)")
    a = ap.parse_args()
    setup_logging()
    if a.cmd == "demo":
        cmd_run("demo", a.port, a.speed, a.seed, a.traders)
    elif a.cmd in ("paper", "live"):
        cmd_run(a.cmd, a.port)
    elif a.cmd == "research":
        cmd_research(a.days, a.max_tx, a.top)
    elif a.cmd == "traders":
        cmd_traders(a.check)
    elif a.cmd == "wallet-check":
        cmd_wallet_check()
    elif a.cmd == "approve":
        cmd_approve()
    elif a.cmd == "ack-risks":
        cmd_ack_risks()
    elif a.cmd == "golive-check":
        cmd_golive_check()
    elif a.cmd in ("kill", "reset"):
        cmd_kill(reset=a.cmd == "reset")


if __name__ == "__main__":
    main()
