"""The final check before real money. Every answer must be clean, or the bot refuses to trade live."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from phantom.calibration import calibration_gate
from phantom.config import file_sha256, load_strategy
from phantom.journal import Journal

KILL_PATHS = ("drawdown", "api_errors", "position_mismatch", "manual")
MIN_PAPER_DAYS = 14
MAX_HOLDOUT_RUNS = 3
KILL_TEST_MAX_AGE = 7 * 86_400
MIN_PAPER_TRADES = 20
HIT_RATE_TOLERANCE = 0.10

BLOW_UP_RISKS = [
    "The bot wallet's private key leaks (malware, a shared screen, a pasted .env). Anyone holding it can empty the wallet. Keep only what you can lose in it.",
    "Your Mac sleeps, loses Wi-Fi or power while holding BTC. Stops live in the bot, not on-chain, so nothing sells until it is back.",
    "BTC gaps through the stop in a crash; the sell fills far below it.",
    "Jupiter, the Solana network or your RPC provider is down or congested; swaps fail or land late.",
    "cbBTC loses its peg to BTC (custodian or bridge problem); the Coinbase price the bot watches no longer matches what it holds.",
    "USDC depegs; your 'cash' is worth less than a dollar.",
    "The strategy was overfit: it passed the backtest by luck and has no real edge.",
    "Jev's probabilities drift out of calibration and Kelly sizes up on false confidence.",
    "A bug in this code. It is new and has only been tested on paper.",
]


@dataclass
class Item:
    question: str
    ok: bool
    detail: str


@dataclass
class Report:
    items: list[Item]

    @property
    def ok(self) -> bool:
        return all(i.ok for i in self.items)

    def text(self) -> str:
        lines = [f"[{'PASS' if i.ok else 'FAIL'}] {i.question}\n       {i.detail}" for i in self.items]
        verdict = "All clean. Live trading may start." if self.ok else "Refusing to go live until every answer is clean."
        return "\n".join(lines + ["", verdict])


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def go_live_report(state_dir: str | Path = "state", reports_dir: str | Path = "reports",
                   risk_path: str | Path = "config/risk.yaml", strategy_path: str | Path = "config/strategy.yaml") -> Report:
    state_dir, reports_dir = Path(state_dir), Path(reports_dir)
    strat = load_strategy(strategy_path)
    items: list[Item] = []

    gate = _load(reports_dir / "gate.json")
    gate_ok = bool(gate and gate.get("passed") and gate.get("strategy_version") == strat.version and gate.get("jev")
                   and int(gate.get("holdout_runs", 99)) <= MAX_HOLDOUT_RUNS and gate.get("jev_cutoff") not in (None, "None"))
    detail = "passed with Jev, out of sample and on the holdout"
    if not gate_ok:
        detail = ("needs a passing report for this strategy version, run WITH Jev (live trades use Jev), with jev_training_cutoff set, "
                  f"graded on the holdout no more than {MAX_HOLDOUT_RUNS} times (run: python -m phantom backtest --jev --holdout)")
    items.append(Item("Did this strategy version pass the backtest gate out of sample?", gate_ok, detail))

    j = Journal(state_dir / "journal.sqlite")
    trades = j.closed_trades("paper", version=strat.version)  # only paper trades of the version being promoted
    days = 0 if not trades else (trades[-1]["exit_t"] - trades[0]["entry_t"]) / 86_400
    paper_hit = sum(1 for t in trades if t["pnl"] > 0) / len(trades) if trades else 0.0
    bt_hit = (gate or {}).get("oos", {}).get("hit_rate")
    match = (days >= MIN_PAPER_DAYS and len(trades) >= MIN_PAPER_TRADES and bt_hit is not None
             and abs(paper_hit - bt_hit) <= HIT_RATE_TOLERANCE)
    items.append(Item("Does paper trading match the backtest?", match,
                      f"{days:.1f} paper days (need {MIN_PAPER_DAYS}), {len(trades)} trades (need {MIN_PAPER_TRADES}), "
                      f"hit rate paper {paper_hit:.0%} vs backtest {bt_hit if bt_hit is None else f'{bt_hit:.0%}'} "
                      f"(tolerance {HIT_RATE_TOLERANCE:.0%})"))

    kt = _load(state_dir / "kill_tests.json") or {}
    fresh = kt.get("_meta", {}).get("risk_sha256") == file_sha256(risk_path) and time.time() - kt.get("_meta", {}).get("at", 0) < KILL_TEST_MAX_AGE
    fired = [p for p in KILL_PATHS if kt.get(p, {}).get("ok")]
    items.append(Item("Did the kill switch fire in testing, through every trigger?", fresh and len(fired) == len(KILL_PATHS),
                      f"fired: {', '.join(fired) or 'none'}; required: {', '.join(KILL_PATHS)}; "
                      f"{'recent, with these limits' if fresh else 'must be re-run within 7 days and after any limit change'} (run: python -m phantom test-kill)"))

    approved = (state_dir / "risk_approved.sha256")
    limits_ok = approved.exists() and approved.read_text().strip() == file_sha256(risk_path)
    items.append(Item("Is every hard limit enforced in code, unchanged since you approved it?", limits_ok,
                      "risk.yaml matches your approval" if limits_ok else "approve the current limits: python -m phantom approve-limits"))

    p, y = j.calibration_samples(mode=None)
    cal_ok, cal_detail = calibration_gate(p, y, int(strat.calibration_gate["min_samples"]), float(strat.calibration_gate["max_ece"]))
    items.append(Item("Is Jev's confidence calibrated on your own decisions?", cal_ok, cal_detail))

    regimes = (gate or {}).get("regimes")
    reg_ok = bool(regimes) and bool((gate or {}).get("checks", {}).get("worst_regime", {}).get("ok"))
    worst = min(regimes.items(), key=lambda kv: kv[1]["avg_r"])[0] if regimes else "unknown"
    items.append(Item("What market regime would break this?", reg_ok,
                      f"weakest regime in the backtest: {worst}" if regimes else "no regime breakdown yet (run the backtest)"))

    ack = _load(state_dir / "ack_risks.json")
    items.append(Item("WHAT COULD BLOW UP THIS ACCOUNT?", bool(ack and ack.get("count") == len(BLOW_UP_RISKS)),
                      "you have read and accepted each risk" if ack else "read and accept the list: python -m phantom ack-risks"))
    return Report(items)
