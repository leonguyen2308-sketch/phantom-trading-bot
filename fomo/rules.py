"""The rule from the video: copy the best trader on the weekly list. After N losing copies in a row,
fire them and copy the next trader on the list who hasn't been fired.

Pure bookkeeping, no I/O: state is a plain dict the bot saves after every change.
"""

from __future__ import annotations

from fomo.leaderboard import Trader


class Rotation:
    def __init__(self, fire_after: int, cooldown_seconds: float, state: dict | None = None):
        self.fire_after, self.cooldown = fire_after, cooldown_seconds
        self.s = state or {"lineup": [], "following": None, "fired": {}, "stats": {}, "stints": [], "fires": 0}

    # ---- reads ----
    @property
    def lineup(self) -> list[Trader]:
        return [Trader(**t) for t in self.s["lineup"]]

    @property
    def following(self) -> Trader | None:
        w = self.s["following"]
        return next((t for t in self.lineup if t.wallet == w), None)

    def is_fired(self, wallet: str, now: float) -> bool:
        return self.s["fired"].get(wallet, 0) > now

    def stats(self, wallet: str) -> dict:
        return self.s["stats"].get(wallet) or {"trades": 0, "wins": 0, "pnl": 0.0, "streak": 0, "last": []}

    # ---- changes ----
    def set_lineup(self, traders: list[Trader], now: float) -> list[str]:
        """Load a fresh weekly top. Keep copying the current trader if they are still on it."""
        self.s["lineup"] = [t.to_dict() for t in traders]
        events = [f"weekly top loaded · {len(traders)} traders"]
        cur = self.s["following"]
        if not cur or cur not in {t.wallet for t in traders} or self.is_fired(cur, now):
            events += self._advance(now, why="")
        return events

    def _advance(self, now: float, why: str) -> list[str]:
        if self.s["stints"] and self.s["stints"][-1]["end"] is None:
            self.s["stints"][-1]["end"] = now
        nxt = next((t for t in self.lineup if not self.is_fired(t.wallet, now)), None)
        self.s["following"] = nxt.wallet if nxt else None
        if not nxt:
            return ["every trader on the weekly top is fired · copying nobody until the list refreshes"]
        self.s["stints"].append({"wallet": nxt.wallet, "handle": nxt.handle, "start": now, "end": None, "pnl": 0.0, "fired": False})
        wr = f" · {nxt.win_rate:.0%} WR" if nxt.win_rate is not None else ""
        return [f"@{nxt.handle} #{nxt.rank} weekly{wr} → follow{why}"]

    def record(self, wallet: str, pnl: float, now: float) -> tuple[bool, list[str]]:
        """A copied position from this trader closed with this P&L. Returns (fired, events)."""
        st = self.stats(wallet)
        st["trades"] += 1
        st["pnl"] += pnl
        win = pnl > 0
        st["wins"] += int(win)
        st["streak"] = 0 if win else st["streak"] + 1
        st["last"] = (st["last"] + ["W" if win else "L"])[-6:]
        st.setdefault("results", [])
        st["results"] = (st["results"] + [round(pnl, 4)])[-6:]
        self.s["stats"][wallet] = st
        for stint in reversed(self.s["stints"]):
            if stint["wallet"] == wallet:
                stint["pnl"] += pnl
                break
        if st["streak"] < self.fire_after:
            return False, []
        handle = next((t.handle for t in self.lineup if t.wallet == wallet), wallet[:6])
        self.s["fired"][wallet] = now + self.cooldown
        self.s["fires"] += 1
        st["streak"] = 0
        for stint in reversed(self.s["stints"]):
            if stint["wallet"] == wallet:
                stint["fired"] = True
                break
        events = [f"@{handle} {self.fire_after} L in a row → fired"]
        if self.s["following"] == wallet:
            events += self._advance(now, why=" (next on the weekly top)")
        return True, events
