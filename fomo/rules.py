"""The rule from the video: copy the best traders on the weekly list. After N losing copies in a row, fire a
trader and copy the next one on the list who hasn't been fired.

`follow_count` traders are copied at once (1 in the first video, 2 in the Grok one: "#1 and #2"); crowd mode
(`follow_all`) copies everyone on the list who isn't fired. The order in which the bench is called up is the
list's own rank, unless an `order` is set (the Grok bot asks Grok to put the bench in order).

Pure bookkeeping, no I/O: state is a plain dict the bot saves after every change.
"""

from __future__ import annotations

from fomo.leaderboard import Trader


class Rotation:
    def __init__(self, fire_after: int, cooldown_seconds: float, state: dict | None = None, follow_all: bool = False,
                 follow_count: int = 1):
        self.fire_after, self.cooldown, self.follow_all = fire_after, cooldown_seconds, follow_all
        self.follow_count = max(1, int(follow_count))
        self.s = state or {"lineup": [], "following": None, "fired": {}, "stats": {}, "stints": [], "fires": 0}
        self.s.setdefault("follows", [self.s["following"]] if self.s.get("following") else [])  # state saved by an older version
        self.s.setdefault("order", [])

    # ---- reads ----
    @property
    def lineup(self) -> list[Trader]:
        return [Trader(**t) for t in self.s["lineup"]]

    @property
    def following(self) -> Trader | None:
        w = self.s["following"]
        return next((t for t in self.lineup if t.wallet == w), None)

    @property
    def follows(self) -> list[Trader]:
        """Everyone copied right now, in the order they were called up (not crowd mode)."""
        by = {t.wallet: t for t in self.lineup}
        return [by[w] for w in self.s["follows"] if w in by]

    def is_fired(self, wallet: str, now: float) -> bool:
        return self.s["fired"].get(wallet, 0) > now

    def active(self, now: float) -> list[Trader]:
        """Who is copied right now. Crowd mode: everyone on the list who isn't fired. Otherwise the followed ones."""
        if not self.follow_all:
            return self.follows
        return [t for t in self.lineup if not self.is_fired(t.wallet, now)]

    def stats(self, wallet: str) -> dict:
        return self.s["stats"].get(wallet) or {"trades": 0, "wins": 0, "pnl": 0.0, "streak": 0, "last": []}

    def bench(self, now: float) -> list[Trader]:
        """Who would be called up next, best first: the set order (Grok's), then the list's rank."""
        pos = {w: i for i, w in enumerate(self.s["order"])}
        free = [t for t in self.lineup if t.wallet not in self.s["follows"] and not self.is_fired(t.wallet, now)]
        return sorted(free, key=lambda t: (pos.get(t.wallet, len(pos)), t.rank))

    # ---- changes ----
    def set_lineup(self, traders: list[Trader], now: float) -> list[str]:
        """Load a fresh weekly top. Keep copying the current traders if they are still on it."""
        self.s["lineup"] = [t.to_dict() for t in traders]
        events = [f"weekly top loaded · {len(traders)} traders"]
        if self.follow_all:
            self.s["following"], self.s["follows"] = None, []  # nobody is "the one": everyone not fired is copied
            return events
        listed = {t.wallet for t in traders}
        for w in [w for w in self.s["follows"] if w not in listed or self.is_fired(w, now)]:
            self._drop(w, now)
        return events + self._fill(now, why="")

    def set_order(self, wallets: list[str], now: float) -> list[str]:
        """Put the bench in this order (best first). Whoever is being copied stays; only who comes next changes."""
        self.s["order"] = [w for w in wallets if isinstance(w, str)]
        return [] if self.follow_all else self._fill(now, why="")

    def _drop(self, wallet: str, now: float) -> None:
        self.s["follows"] = [w for w in self.s["follows"] if w != wallet]
        for stint in reversed(self.s["stints"]):
            if stint["wallet"] == wallet and stint["end"] is None:
                stint["end"] = now
                break
        self.s["following"] = self.s["follows"][0] if self.s["follows"] else None

    def _fill(self, now: float, why: str) -> list[str]:
        events = []
        for nxt in self.bench(now)[: max(0, self.follow_count - len(self.s["follows"]))]:
            self.s["follows"].append(nxt.wallet)
            self.s["stints"].append({"wallet": nxt.wallet, "handle": nxt.handle, "start": now, "end": None, "pnl": 0.0, "fired": False})
            wr = f" · {nxt.win_rate:.0%} WR" if nxt.win_rate is not None else ""
            events.append(f"@{nxt.handle} #{nxt.rank} weekly{wr} → follow{why}")
        self.s["following"] = self.s["follows"][0] if self.s["follows"] else None
        if not self.s["follows"]:
            events.append("every trader on the weekly top is fired · copying nobody until the list refreshes")
        return events

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
        if wallet in self.s["follows"]:
            self._drop(wallet, now)
            events += self._fill(now, why=" (next on the weekly top)")
        return True, events
