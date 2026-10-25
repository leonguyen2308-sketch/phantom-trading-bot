"""What must be true before the copy bot may spend real money. Each check reads evidence on disk."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from fomo.journal import Journal
from phantom.config import file_sha256

MIN_PAPER_HOURS = 24
MIN_PAPER_COPIES = 5

BLOW_UP_RISKS = [
    "Most people who copy memecoin traders lose money. DWF Ventures found about 94% of FOMO wallets were not in "
    "profit over three months. The video you saw is an ad: it does not show a real account.",
    "Your copy always lands a few seconds after the trader's. Other copiers pump the price before you buy and dump it "
    "after the trader sells, so you can lose on trades the trader wins.",
    "A memecoin can go to zero in one block (rug pull). The stop loss sells after the drop, not before it.",
    "The trader can be paid to promote a coin, or can sell into their own followers. Their weekly rank says nothing "
    "about next week.",
    "The bot wallet's private key is in .env on this Mac. Anyone who gets it can empty the wallet. Keep only what you "
    "can lose there, and never use your main Phantom wallet.",
    "If the Mac sleeps, loses internet or the RPC is down, the bot misses the trader's sells and its own stop losses.",
    "FOMO wallet lookups come from unofficial third-party tools. A wrong wallet means copying a stranger.",
    "This code is new. It has been tested on simulations and unit tests, not on months of real trades.",
]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check]
    paper_summary: str

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def text(self) -> str:
        lines = ["FOMO copy bot: go-live check", ""]
        lines += [f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks]
        lines += ["", f"  Paper results so far: {self.paper_summary}", "",
                  "  READY FOR LIVE" if self.ok else "  NOT READY. Fix every FAIL above first."]
        return "\n".join(lines)


def paper_summary(j: Journal) -> tuple[float, int, str]:
    curve = j.equity_curve("paper")
    hours = (curve[-1]["t"] - curve[0]["t"]) / 3600 if len(curve) > 1 else 0.0
    copies = j.count_copies("paper")
    closed = j.closed_copies("paper")
    wins = sum(1 for c in closed if (c["pnl"] or 0) > 0)
    pnl = sum(c["pnl"] or 0 for c in closed)
    start = float(j.get("start:paper") or 0)
    eq = float(j.get("equity:paper") or 0)
    s = (f"{hours:.0f}h, {copies} copies, {len(closed)} closed ({wins} won), realized {pnl:+.2f} USD, "
         f"equity {start:.2f} -> {eq:.2f} USD")
    return hours, copies, s


def go_live_report(state: Path, config: Path, prefix: str = "fomo", env: str = "FOMO") -> Report:
    """prefix/env: "fomo"/"FOMO" for the FOMO copy bot, "grok"/"GROK" for the Grok bot (its own files and wallet)."""
    checks: list[Check] = []
    approved = state / f"{prefix}_approved.sha256"
    ok = approved.exists() and config.exists() and approved.read_text().strip() == file_sha256(config)
    checks.append(Check("limits approved", ok, f"{config} approved and unchanged" if ok else f"run ./{prefix}.sh approve"))
    ack = state / f"{prefix}_ack_risks.json"
    ok = ack.exists() and json.loads(ack.read_text()).get("count") == len(BLOW_UP_RISKS)
    checks.append(Check("risks read", ok, "every risk accepted" if ok else f"run ./{prefix}.sh ack-risks"))
    hours, copies, summary = paper_summary(Journal(state / f"{prefix}.sqlite"))
    ok = hours >= MIN_PAPER_HOURS and copies >= MIN_PAPER_COPIES
    checks.append(Check("paper run works", ok, f"{hours:.0f}h and {copies} copies on paper (need {MIN_PAPER_HOURS}h and "
                                                f"{MIN_PAPER_COPIES}: proves your RPC sees the trades and Jupiter fills them)"))
    keys = [k for k in (f"{env}_BOT_PRIVATE_KEY", f"{env}_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL") if not os.environ.get(k)]
    checks.append(Check("keys in .env", not keys, "all set" if not keys else f"missing {', '.join(keys)}"))
    ok = os.environ.get(f"{env}_LIVE") == "yes"
    checks.append(Check(f"{env}_LIVE=yes", ok, "set in .env" if ok else f"set {env}_LIVE=yes in .env when you are sure"))
    return Report(checks, summary)
