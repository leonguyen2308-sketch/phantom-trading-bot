"""What must be true before the desk may spend real money. Every check reads evidence on disk.

The paper record has to be long enough to mean something AND positive after costs: if the desk loses money
with fake money, it will lose money with real money.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from desk.journal import Journal, trade_stats
from phantom.config import file_sha256

MIN_PAPER_HOURS = 72
MIN_PAPER_TRADES = 30

BLOW_UP_RISKS = [
    "About 74% of Solana memecoin buyers end with less money than they started (Bitquery, 2.9M buyers), and 1% of "
    "buyers take over half the gains. The videos you saw are self-reported marketing; one creator's guide even "
    "hard-codes his referral link into the order path.",
    "Most memecoins rug or go to zero. A stop loss sells after the drop, not before it: a coin can lose most of its "
    "value in one block, so every position is sized as if it can go to zero.",
    "The Claude desk can be wrong, and nobody has shown that LLMs add an edge in memecoins. The dashboard's "
    "'desk vs rules' panel is there to find out; if the desk does not beat rules-only, it is not earning its cost.",
    "AI calls cost money every day whether trades win or lose. The daily AI budget in config/desk.yaml caps it.",
    "The bot wallet's private key is in .env on this Mac. Anyone who gets it can empty the wallet. Keep only what you "
    "can lose there, and never use your main Phantom wallet.",
    "If the Mac sleeps, loses internet or an API is down, stops and take-profits do not run until it is back.",
    "Trading on unlicensed or offshore platforms is a legal grey area in Vietnam (Decree 284/2026 sets fines that "
    "officials say apply once the first licensed exchange exists). Crypto transfers may also be taxed.",
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
    summary: str

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def text(self) -> str:
        lines = ["Memecoin Desk: go-live check", ""]
        lines += [f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks]
        lines += ["", f"  Paper record: {self.summary}", "", "  READY FOR LIVE" if self.ok else "  NOT READY. Fix every FAIL above first."]
        return "\n".join(lines)


def go_live_report(state: Path, config: Path) -> Report:
    checks: list[Check] = []
    approved = state / "desk_approved.sha256"
    ok = approved.exists() and config.exists() and approved.read_text().strip() == file_sha256(config)
    checks.append(Check("rules approved", ok, "config/desk.yaml approved and unchanged" if ok else "run ./desk.sh approve"))
    ack = state / "desk_ack_risks.json"
    ok = ack.exists() and json.loads(ack.read_text()).get("count") == len(BLOW_UP_RISKS)
    checks.append(Check("risks read", ok, "every risk accepted" if ok else "run ./desk.sh ack-risks"))
    j = Journal(state / "desk.sqlite")
    curve = j.equity_curve("paper")
    hours = (curve[-1]["t"] - curve[0]["t"]) / 3600 if len(curve) > 1 else 0.0
    st = trade_stats(j.closed_positions("paper", "real"))
    n = st.get("n", 0)
    checks.append(Check("paper long enough", hours >= MIN_PAPER_HOURS and n >= MIN_PAPER_TRADES,
                        f"{hours:.0f}h and {n} closed trades on paper (need {MIN_PAPER_HOURS}h and {MIN_PAPER_TRADES})"))
    pnl = st.get("pnl_usd", 0.0)
    checks.append(Check("paper made money", n >= MIN_PAPER_TRADES and pnl > 0,
                        f"paper P&L {pnl:+.2f} USD after costs" + ("" if pnl > 0 else ": a desk that loses on paper loses with real money")))
    keys = [k for k in ("DESK_BOT_PRIVATE_KEY", "DESK_BOT_ADDRESS", "JUPITER_API_KEY", "SOLANA_RPC_URL") if not os.environ.get(k)]
    checks.append(Check("keys in .env", not keys, "all set" if not keys else f"missing {', '.join(keys)}"))
    ok = os.environ.get("DESK_LIVE") == "yes"
    checks.append(Check("DESK_LIVE=yes", ok, "set in .env" if ok else "set DESK_LIVE=yes in .env when you are sure"))
    sh = trade_stats(j.closed_positions("paper", "shadow"))
    summary = (f"{hours:.0f}h · {n} trades · expectancy {st.get('expectancy_pct', 0):+.1f}%/trade "
               f"(95% range {st.get('ci95_pct')}) · rules-only shadow {sh.get('expectancy_pct', 0):+.1f}%/trade over {sh.get('n', 0)}")
    return Report(checks, summary)
