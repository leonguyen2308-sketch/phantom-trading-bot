"""The exit plan, written before entry and never loosened ("TP-first").

At entry the bot fixes, in priority order:
1. a hard stop from volatility (expect it to gap: memecoins can drop most of their value in one block);
2. ladder take-profits (LADDER) that each sell a fixed share of the original position;
3. a trailing stop that switches on only after the first rung fills, never below breakeven;
4. a BOOK target that sells everything left;
5. an EXIT when volume dies, and a time stop if the trade still isn't profitable.
The stop only ever moves up; targets never move. Prices are USD per whole token.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class PlanRules:
    stop_atr_mult: float
    min_stop_pct: float
    max_stop_pct: float
    ladder_pct: tuple[float, ...]  # gains that trigger each rung, e.g. (50, 100)
    ladder_fraction: float  # share of the ORIGINAL position each rung sells
    trail_pct: float  # after rung 1: sell if price falls this far below its peak
    book_pct: float | None  # sell everything left at this gain (None = trail only)
    volume_death_ratio: float
    volume_death_checks: int
    unprofitable_hours: float  # time stop while the trade is still not profitable
    max_hold_hours: float  # absolute limit
    min_first_rung_r: float  # first rung must be at least this many stop-distances away
    round_trip_cost_pct: float


@dataclass
class ExitPlan:
    entry: float
    stop: float  # current stop: only ever moves up
    initial_stop: float
    rungs: list[float]
    rung_fraction: float
    trail_pct: float
    book: float | None
    entry_vol_5m: float
    volume_death_ratio: float
    volume_death_checks: int
    unprofitable_s: float
    max_hold_s: float
    breakeven: float  # entry plus round-trip costs
    peak: float = 0.0
    rungs_done: int = 0
    low_volume_count: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def stop_pct(self) -> float:
        return (self.entry - self.initial_stop) / self.entry * 100

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExitPlan":
        return cls(**d)


def make_plan(entry: float, atr_pct: float | None, entry_vol_5m: float, rules: PlanRules,
              strict: bool = True) -> tuple[ExitPlan | None, list[str]]:
    """The plan for a buy at `entry`, or None with the reasons it isn't worth taking. strict=False always returns a
    plan (for coins already bought: an exit plan is needed whatever the rules now say about entering)."""
    if entry <= 0:
        return None, ["no entry price"]
    base = (atr_pct or 0) * rules.stop_atr_mult
    stop_pct = min(max(base, rules.min_stop_pct), rules.max_stop_pct)
    why = []
    if rules.ladder_fraction * len(rules.ladder_pct) > 1 + 1e-9:
        why.append("the ladder sells more than the whole position")
    if rules.ladder_pct and rules.ladder_pct[0] < rules.min_first_rung_r * stop_pct:
        why.append(f"first take-profit +{rules.ladder_pct[0]:g}% is under {rules.min_first_rung_r:g}x the {stop_pct:.0f}% stop")
    if stop_pct <= rules.round_trip_cost_pct * 3:
        why.append(f"stop {stop_pct:.1f}% is too close to the {rules.round_trip_cost_pct:.1f}% round-trip cost")
    if why and strict:
        return None, why
    stop = entry * (1 - stop_pct / 100)
    plan = ExitPlan(entry=entry, stop=stop, initial_stop=stop, rungs=[entry * (1 + p / 100) for p in rules.ladder_pct],
                    rung_fraction=rules.ladder_fraction, trail_pct=rules.trail_pct,
                    book=entry * (1 + rules.book_pct / 100) if rules.book_pct else None, entry_vol_5m=entry_vol_5m,
                    volume_death_ratio=rules.volume_death_ratio, volume_death_checks=rules.volume_death_checks,
                    unprofitable_s=rules.unprofitable_hours * 3600, max_hold_s=rules.max_hold_hours * 3600,
                    breakeven=entry * (1 + rules.round_trip_cost_pct / 100), peak=entry)
    return plan, []


@dataclass
class Action:
    kind: str  # "stop", "trail", "rung", "book", "exit" (volume died), "time"
    fraction: float  # share of what is LEFT to sell now (1.0 = everything)
    note: str


def _raise_stop(plan: ExitPlan, new_stop: float, why: str) -> None:
    if new_stop > plan.stop:
        plan.stop = new_stop
        plan.history.append(f"stop raised to {new_stop:.6g} ({why})")


def step(plan: ExitPlan, price: float, vol_5m: float | None, held_s: float, left_fraction: float,
         volume_sample: bool = False) -> Action | None:
    """What to sell at this price check, if anything. Updates the plan's peak, stop and counters.

    `left_fraction` is the share of the original position still held. `volume_sample` is True when
    `vol_5m` is a fresh 5-minute reading (volume is judged only on those, not on every price tick).
    """
    if price <= 0 or left_fraction <= 0:
        return None
    plan.peak = max(plan.peak, price)
    if plan.rungs_done > 0:  # trailing stop is live after the first take-profit, and never below breakeven
        _raise_stop(plan, max(plan.peak * (1 - plan.trail_pct / 100), plan.breakeven), "trailing")
    if price <= plan.stop:
        kind = "stop" if plan.rungs_done == 0 else "trail"
        return Action(kind, 1.0, f"price {price:.6g} hit the {'stop' if kind == 'stop' else 'trailing stop'} {plan.stop:.6g}")
    if plan.book is not None and price >= plan.book:
        return Action("book", 1.0, f"price {price:.6g} reached the book target {plan.book:.6g}")
    if plan.rungs_done < len(plan.rungs) and price >= plan.rungs[plan.rungs_done]:
        k = plan.rungs_done
        plan.rungs_done += 1
        _raise_stop(plan, max(plan.peak * (1 - plan.trail_pct / 100), plan.breakeven), f"rung {k + 1} filled")
        sell = min(plan.rung_fraction / left_fraction, 1.0)
        return Action("rung", sell, f"take-profit {k + 1} at {plan.rungs[k]:.6g}: selling {plan.rung_fraction:.0%} of the original")
    if volume_sample and vol_5m is not None and plan.entry_vol_5m > 0:
        if vol_5m < plan.entry_vol_5m * plan.volume_death_ratio:
            plan.low_volume_count += 1
            if plan.low_volume_count >= plan.volume_death_checks:
                return Action("exit", 1.0, f"5m volume fell to {vol_5m / plan.entry_vol_5m:.0%} of its level at entry: the move died")
        else:
            plan.low_volume_count = 0
    if held_s >= plan.max_hold_s:
        return Action("time", 1.0, f"held {held_s / 3600:.1f}h, the absolute limit")
    if held_s >= plan.unprofitable_s and price < plan.breakeven:
        return Action("time", 1.0, f"still not profitable after {held_s / 3600:.1f}h")
    return None
