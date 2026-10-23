"""Command line for the Memecoin Desk: python -m desk <command> (or ./desk.sh <command>)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from phantom.config import file_sha256, install_redaction, load_env

STATE = Path("state")
CONFIG = Path("config/desk.yaml")
WALLETS = Path("config/desk_wallets.yaml")
PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "desk.html"
log = logging.getLogger("desk")


def adopt_old_kill(state: Path, kill) -> None:
    """Versions before the per-mode switches kept one state/desk_kill.json: a switch tripped there stays tripped."""
    from phantom.risk import KillSwitch
    old = KillSwitch(state / "desk_kill.json")
    if old.path.exists() and old.tripped and not kill.tripped:
        kill.trip(old.reason or "kill switch tripped before the upgrade")


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler(sys.stdout), RotatingFileHandler("logs/desk.log", maxBytes=5_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_redaction([os.environ.get("ANTHROPIC_API_KEY", ""), os.environ.get("DESK_BOT_PRIVATE_KEY", "")])


def need(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. See HUONG_DAN_DESK.md.")


def load_keypair():
    from solders.keypair import Keypair
    try:
        kp = Keypair.from_base58_string(os.environ["DESK_BOT_PRIVATE_KEY"].strip())
    except Exception:
        sys.exit("DESK_BOT_PRIVATE_KEY is not a valid Solana private key (Phantom: Settings -> wallet -> Show Private Key).")
    if str(kp.pubkey()) != os.environ["DESK_BOT_ADDRESS"].strip():
        sys.exit(f"DESK_BOT_PRIVATE_KEY belongs to {kp.pubkey()}, not DESK_BOT_ADDRESS. Fix .env.")
    return kp


def tracked_wallets() -> dict[str, str]:
    if not WALLETS.exists():
        return {}
    from fomo.leaderboard import is_wallet
    out = {}
    for row in (yaml.safe_load(WALLETS.read_text()) or {}).get("wallets") or []:
        w = str(row.get("wallet", "")).strip()
        if not is_wallet(w):
            sys.exit(f"config/desk_wallets.yaml: '{w}' is not a Solana wallet address")
        out[w] = str(row.get("name") or w[:6])
    return out


def real_sources(cfg):
    """The live data sources. One set of per-host rate limits (Jupiter's free key is ~1 request/second for everything),
    shared by two clients: the scanner's, which may wait out a 429 for up to 30 s, and the manager's (prices for
    open positions), which waits at most 2 s so a stop is never held up by the scanner's rate limit."""
    from desk.sources import DexScreener, GeckoTerminal, Holders, JupiterTokens, LimitedClient, RugCheck
    from fomo.chain import SolanaRpc
    from fomo.market import Prices
    http = LimitedClient()
    mhttp = LimitedClient(share=http, max_wait=2.0)
    bhttp = LimitedClient(share=http, max_wait=5.0)  # quotes and swaps: a stop's sell never waits 30 s on a 429
    rpc = SolanaRpc(os.environ["SOLANA_RPC_URL"])
    key = os.environ.get("JUPITER_API_KEY", "")
    return {"http": http, "rpc": rpc, "jup": JupiterTokens(key, http), "dex": DexScreener(http), "gecko": GeckoTerminal(http),
            "rug": RugCheck(http), "holders": Holders(rpc), "mint_info": rpc.mint_info, "prices": Prices(key, mhttp, max_age=30),
            "mdex": DexScreener(mhttp), "bhttp": bhttp}


# ---------------------------------------------------------------- run

def cmd_run(mode: str, port: int, speed: float = 1.0, seed: int | None = None) -> None:
    from desk.bot import Desk
    from desk.config import load_config
    from desk.journal import Journal
    from phantom.risk import KillSwitch
    from phantom.server import make_app, serve_in_background

    cfg = load_config(CONFIG)
    if mode == "demo":  # the made-up market has coins of every size: show the strict funnel and the desk, not the hot band
        cfg = dataclasses.replace(cfg, hot=dataclasses.replace(cfg.hot, enabled=False), desk=dataclasses.replace(cfg.desk, enabled=True))
    cfg_hash = file_sha256(CONFIG)
    STATE.mkdir(exist_ok=True)
    tick = 1.0
    watcher, tracked = None, {}
    if mode == "demo":
        from desk import sim
        for f in (STATE / "desk_demo.sqlite", STATE / "desk_demo_kill.json"):
            f.unlink(missing_ok=True)
        market = sim.SimMarket(seed=seed, minutes_per_step=max(speed / 60, 0.05))
        bot = Desk(mode="demo", cfg=cfg, broker=sim.SimBroker(market, 100.0, cfg.paper.slippage_bps), jup=sim.SimJup(market),
                   dex=sim.SimDex(market), gecko=sim.SimGecko(market), rug=sim.SimRug(market), holders=sim.SimHolders(market),
                   mint_info=sim.sim_mint_info(market), prices=sim.SimPrices(market), desk=sim.RuleDesk(random.Random(seed)),
                   journal=Journal(STATE / "desk_demo.sqlite"), kill=KillSwitch(STATE / "desk_demo_kill.json"), clock=market.clock,
                   threaded=False)
        print(f"SIMULATION at {speed:g}x: made-up tokens, a rule-based stand-in for the Claude desk, fake money. Nothing is real.")
    else:
        need("SOLANA_RPC_URL", "JUPITER_API_KEY")
        src = real_sources(cfg)
        journal, kill = Journal(STATE / "desk.sqlite"), KillSwitch(STATE / f"desk_{mode}_kill.json")  # paper's limits never sell live
        adopt_old_kill(STATE, kill)
        sell_only = None
        if mode == "live":
            from desk.golive import go_live_report
            approved = STATE / "desk_approved.sha256"
            if not approved.exists() or approved.read_text().strip() != cfg_hash:
                sys.exit("config/desk.yaml is not approved (or changed since). Run: ./desk.sh approve")
            report = go_live_report(STATE, CONFIG)
            print(report.text())
            if not report.ok:
                if not journal.open_positions("live") and not journal.pending("live"):
                    sys.exit(1)
                sell_only = "go-live checks no longer pass: selling what is open, buying nothing"  # a reset can't lift it
                kill.trip("Go-live checks no longer pass while live positions are open; selling them")
            from fomo.brokers import LiveBroker
            kp = load_keypair()
            broker = LiveBroker(kp, os.environ["JUPITER_API_KEY"], src["rpc"], http=src["bhttp"])
            log.info("LIVE from bot wallet %s", kp.pubkey())
        else:
            from fomo.brokers import PaperBroker
            start = float(os.environ.get("DESK_PAPER_START_USD", "100"))
            broker = PaperBroker(STATE / "desk_paper.json", os.environ["JUPITER_API_KEY"], start, cfg.paper.slippage_bps, http=src["bhttp"])
        if cfg.desk.enabled and os.environ.get("ANTHROPIC_API_KEY"):
            from desk.ai import ClaudeDesk
            brain = ClaudeDesk(cfg.desk, journal, mode=mode)
        else:
            from desk.ai import DeskUnavailable

            class NoDesk:
                def spent_today(self):
                    return 0.0

                def review(self, *a, **k):
                    raise DeskUnavailable("no ANTHROPIC_API_KEY in .env" if cfg.desk.enabled else "desk disabled in config")
            brain = NoDesk()
            log.warning("No Claude desk: %s", "ANTHROPIC_API_KEY missing" if cfg.desk.enabled else "disabled in config")
        if cfg.track.enabled:
            tracked = tracked_wallets()
            if tracked:
                from fomo.chain import ws_url
                from fomo.watcher import LeaderWatcher
                watcher = LeaderWatcher(src["rpc"], src["prices"].sol_usd, os.environ.get("SOLANA_WS_URL") or ws_url(os.environ["SOLANA_RPC_URL"]),
                                        max_age_seconds=max(cfg.track.max_signal_age_seconds * 2, 120))
        bot = Desk(mode=mode, cfg=cfg, broker=broker, jup=src["jup"], dex=src["dex"], gecko=src["gecko"], rug=src["rug"],
                   holders=src["holders"], mint_info=src["mint_info"], prices=src["prices"], desk=brain, journal=journal, kill=kill,
                   watcher=watcher, tracked=tracked, mdex=src["mdex"])
        bot.sell_only = sell_only

    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") and mode != "demo":
        from phantom.alerts import Telegram
        bot.alerts = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

        def status() -> str:
            with bot.lock:
                s = bot.dashboard_state()
            return (f"{mode.upper()} | balance ${s['balance']:,.2f} ({s['pnl']:+,.2f}) | {len(s['positions'])} open | "
                    f"AI ${s['ai']['spent']:.2f}/{s['ai']['budget']:g} | {'KILLED: ' + s['killReason'] if s['killed'] else s['blocked'] or 'trading'}")

        def tg_kill() -> str:
            if not bot.kill.tripped:
                bot.trip("Telegram /kill")
            return "Kill switch fired. Selling everything; buying stopped."
        bot.alerts.listen({"/status": status, "/kill": tg_kill})
        bot._alert(f"Memecoin Desk started in {mode.upper()} mode. Send /status or /kill.")

    serve_in_background(make_app(bot.dashboard_state, lambda r: bot.trip(r, sell=False), bot.lock, page=PAGE), port)
    log.info("Dashboard: http://localhost:%d", port)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())  # launchctl unload: finish the swaps in flight first
    bot.start()
    last_cfg_check = 0.0
    drain_until = None
    while True:
        if stopping.is_set():
            if drain_until is None:
                log.info("Stopping: no new buys; waiting for swaps in flight")
                bot.draining = True
                bot.stop()
                drain_until = time.monotonic() + 120
            if not bot.busy() or time.monotonic() > drain_until:
                log.info("Stopped.")
                return
        if mode == "demo":
            market.advance()
        if mode == "live" and time.monotonic() - last_cfg_check > 15:
            last_cfg_check = time.monotonic()
            try:
                changed = file_sha256(CONFIG) != cfg_hash
            except OSError:
                changed = True
            if changed and not bot.kill.tripped:
                bot.trip("config/desk.yaml changed while running live")
        try:
            bot.step()
        except Exception as e:  # never die with positions open: log, count, keep going
            log.exception("Loop error")
            bot._api_error(f"loop: {type(e).__name__}: {e}")
        else:
            bot._api_ok("general")
        time.sleep(0.2 if mode == "demo" else tick)


# ---------------------------------------------------------------- tools

def cmd_scan() -> None:
    """One sweep against the real market, no trading and no Claude: see the funnel and what passes."""
    need("SOLANA_RPC_URL", "JUPITER_API_KEY")
    from desk.bot import Desk
    from desk.config import load_config
    from desk.journal import Journal
    from fomo.brokers import PaperBroker
    from phantom.risk import KillSwitch
    import tempfile
    cfg = load_config(CONFIG)
    src = real_sources(cfg)
    d = Path(tempfile.mkdtemp())
    from desk.ai import DeskUnavailable

    class NoDesk:
        def spent_today(self):
            return 0.0

        def review(self, *a, **k):
            raise DeskUnavailable("scan only: the desk is not called")
    broker = PaperBroker(d / "p.json", os.environ["JUPITER_API_KEY"], 100.0, cfg.paper.slippage_bps, http=src["http"])
    bot = Desk(mode="scan", cfg=cfg, broker=broker, jup=src["jup"], dex=src["dex"], gecko=src["gecko"], rug=src["rug"],
               holders=src["holders"], mint_info=src["mint_info"], prices=src["prices"], desk=NoDesk(), journal=Journal(d / "j.sqlite"),
               kill=KillSwitch(d / "k.json"), threaded=False)
    bot.equity_now = 100.0
    print("Sweeping the market once (about a minute; free APIs are rate limited)...")
    bot.sweep()
    s = bot.dashboard_state()
    print("\nTHE FUNNEL (this sweep):")
    for g in ("universe", "market", "onchain", "depth", "window"):
        print(f"  {g:9} {s['funnel'].get(g, 0):5d}")
    print("\nWhy tokens died:")
    for k in s["kills"]:
        print(f"  {k['gate']:8} {k['n']:4d}  {k['reason']}")
    print("\nFinalists (passed every code gate):")
    for e in reversed(s["feed"]):
        if e["kind"] in ("PICK", "KILL"):
            print(f"  [{e['kind']}] {e['text']}")
    print("\nNothing was bought and Claude was not called.")


def cmd_desk_test() -> None:
    """One real review by the Claude desk of a made-up token, to check the key, the model and the cost."""
    need("ANTHROPIC_API_KEY")
    from desk.ai import ClaudeDesk, DeskUnavailable
    from desk.config import load_config
    from desk.features import LEGEND
    cfg = load_config(CONFIG)
    snap = {"age_min": 180, "mcap_usd": 2.4e6, "liquidity_usd": 180e3, "liq_to_mcap": 0.075, "holder_count": 2900, "holder_change_1h_pct": 6.5,
            "organic_score": 71, "organic_buy_share_1h": 0.62, "traders_1h": 840, "vol_5m_usd": 48e3, "vol_1h_usd": 410e3, "vol_6h_usd": 1.9e6,
            "vol_accel": 1.4, "vol_to_mcap_1h": 0.17, "buys_5m": 160, "sells_5m": 120, "buys_1h": 1500, "sells_1h": 1250, "buy_sell_ratio_1h": 1.2,
            "price_change_5m_pct": 3.1, "price_change_1h_pct": 22, "price_change_6h_pct": 65, "price_change_24h_pct": 140, "atr_5m_pct": 4.2,
            "drop_from_high_pct": 6, "minutes_since_high": 25, "return_30m_pct": 9, "return_2h_pct": 31, "up_candles_last_12": 7,
            "top1_pct": 3.1, "top10_pct": 18, "fresh_share_pct": 2, "dev_balance_pct": 0, "dev_mints": 1, "rugcheck_risk": 12, "boosted": 0,
            "social_links": 3, "impact_pct": 0.6, "round_trip_loss_pct": 1.9}
    plan = {"entry": 0.0024, "stop_pct": 15, "take_profits_pct": [50, 100], "take_profit_share_each": 0.33, "trail_pct_after_first_take_profit": 30,
            "book_pct": 300, "round_trip_cost_pct": 2.0, "max_hold_hours": 24}
    print(f"Asking {cfg.desk.model} to review a made-up token (analyst + skeptic{' + news' if cfg.desk.news else ''})...")
    try:
        from desk.journal import Journal
        o = ClaudeDesk(cfg.desk, Journal(STATE / "desk.sqlite"), mode="paper").review(
            LEGEND, snap, plan, symbol="TEST", mint="So11111111111111111111111111111111111111112")
    except DeskUnavailable as e:
        sys.exit(f"Desk unavailable: {e}")
    print(json.dumps(o.to_dict(), indent=1))
    print(f"\nCost of this review: ${o.cost_usd:.4f} · {o.latency_ms / 1000:.1f}s. "
          f"With a ${cfg.desk.daily_budget_usd:g}/day budget that is about {int(cfg.desk.daily_budget_usd / max(o.cost_usd, 1e-6))} reviews a day.")


def cmd_wallet_check() -> None:
    need("DESK_BOT_PRIVATE_KEY", "DESK_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL")
    from fomo.brokers import LiveBroker
    from fomo.chain import SolanaRpc
    from fomo.swaps import WSOL
    kp = load_keypair()
    b = LiveBroker(kp, os.environ["JUPITER_API_KEY"], SolanaRpc(os.environ["SOLANA_RPC_URL"]))
    usdc, sol = b.usdc(), b.sol()
    print(f"Bot wallet {kp.pubkey()}\n  USDC {usdc:,.2f}\n  SOL  {sol:.4f}")
    if sol < 0.1:
        print("  ! Keep about 0.1 SOL here: every new coin locks ~0.002 SOL in a token account.")
    if usdc < 3:
        print("  ! Send USDC (Solana network) to this wallet: the desk buys with USDC.")
    q = b.quote_buy(WSOL, 5.0)
    print(f"  Quote check: $5 USDC -> {q.raw / 1e9:.5f} SOL (impact {q.impact_pct:.2f}%). Nothing was sent.")


def cmd_stats(mode: str) -> None:
    from desk.journal import Journal, trade_stats
    j = Journal(STATE / "desk.sqlite")
    for book in ("real", "shadow"):
        st = trade_stats(j.closed_positions(mode, book))
        label = "desk (real trades)" if book == "real" else "rules-only (shadow)"
        print(f"{label:22} {json.dumps(st)}")
    print("exits:", [(r["kind"], r["n"], round(r["pnl"] or 0, 2)) for r in j.exits_by_kind(mode)])


def cmd_approve() -> None:
    from desk.config import load_config
    load_config(CONFIG)
    print(CONFIG.read_text())
    if input("Type APPROVE to lock in these rules and limits: ").strip() != "APPROVE":
        sys.exit("Not approved.")
    STATE.mkdir(exist_ok=True)
    (STATE / "desk_approved.sha256").write_text(file_sha256(CONFIG))
    print("Approved. Live trading refuses to start, and stops, if this file changes.")


def cmd_ack_risks() -> None:
    from desk.golive import BLOW_UP_RISKS
    print("WHAT COULD BLOW UP THIS ACCOUNT?\n")
    for i, r in enumerate(BLOW_UP_RISKS, 1):
        print(f"{i}. {r}")
        if input("   Type I ACCEPT: ").strip() != "I ACCEPT":
            sys.exit("Stopped. Live trading stays off.")
    STATE.mkdir(exist_ok=True)
    (STATE / "desk_ack_risks.json").write_text(json.dumps({"count": len(BLOW_UP_RISKS), "at": int(time.time())}))
    print("Recorded.")


def cmd_golive_check() -> None:
    from desk.golive import go_live_report
    report = go_live_report(STATE, CONFIG)
    print(report.text())
    sys.exit(0 if report.ok else 1)


def cmd_kill(reset: bool) -> None:
    from desk.journal import Journal
    from phantom.risk import KillSwitch
    switches = {m: KillSwitch(STATE / f"desk_{m}_kill.json") for m in ("paper", "live")}
    if not reset:
        for ks in switches.values():
            ks.trip("Command line: ./desk.sh kill")
        print("Kill switch ON (paper and live). The running desk sells everything and stops buying within seconds.")
        return
    for m, ks in switches.items():
        print(f"{m}: kill switch is {'ON: ' + ks.reason if ks.tripped else 'off'}.")
    answer = input("Type RESET to allow trading again: ").strip()
    old = KillSwitch(STATE / "desk_kill.json")
    for ks in [*switches.values(), *([old] if old.path.exists() else [])]:
        ks.reset(answer)
    j = Journal(STATE / "desk.sqlite")
    for m in ("paper", "live"):  # fresh error counts and baselines, or the old ones re-trip it at once
        j.put(f"api_errors:{m}", 0)
        j.put(f"reset_at:{m}", time.time())  # a running desk reads this and forgets its streaks too
        for k in ("peak", "day_start", "start"):  # after a withdrawal, the new balance is the baseline
            j.put(f"{k}:{m}", None)
    print("Kill switch reset. Restart the desk (scripts/install-desk-service.sh, or ./desk.sh paper|live).")


def main() -> None:
    load_env(".env")
    ap = argparse.ArgumentParser(prog="./desk.sh", description="Memecoin Desk: funnel + Claude desk + TP-first exits, on Solana")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="simulation with the dashboard: no keys, fake money")
    d.add_argument("--speed", type=float, default=60.0, help="simulated seconds per step x 0.2s (default 60: about 5 simulated minutes a second)")
    d.add_argument("--seed", type=int, default=None)
    d.add_argument("--port", type=int, default=8002)
    for m in ("paper", "live"):
        sp = sub.add_parser(m, help=f"run on the real market with {'fake' if m == 'paper' else 'REAL'} money")
        sp.add_argument("--port", type=int, default=8002)
    sub.add_parser("scan", help="one sweep of the real market, no trading, no AI: see the funnel")
    sub.add_parser("desk-test", help="one real Claude review of a made-up token: checks the key and the cost")
    sub.add_parser("wallet-check", help="check the bot wallet and a Jupiter quote (sends nothing)")
    st = sub.add_parser("stats", help="desk vs rules-only results so far")
    st.add_argument("--mode", default="paper", choices=["paper", "live"])
    sub.add_parser("approve", help="review and lock config/desk.yaml")
    sub.add_parser("ack-risks", help="read and accept what could blow up the account")
    sub.add_parser("golive-check", help="every check before real money")
    sub.add_parser("kill", help="sell everything and stop buying")
    sub.add_parser("reset", help="reset the kill switch (asks for confirmation)")
    a = ap.parse_args()
    setup_logging()
    if a.cmd == "demo":
        cmd_run("demo", a.port, a.speed, a.seed)
    elif a.cmd in ("paper", "live"):
        cmd_run(a.cmd, a.port)
    elif a.cmd == "scan":
        cmd_scan()
    elif a.cmd == "desk-test":
        cmd_desk_test()
    elif a.cmd == "wallet-check":
        cmd_wallet_check()
    elif a.cmd == "stats":
        cmd_stats(a.mode)
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
