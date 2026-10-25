"""config/fomo.yaml, read into typed settings. Unknown or missing keys are an error, not a silent default."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CopyRules:
    copy_ratio: float
    min_trade_usd: float
    max_trade_usd: float
    exit_with_trader: bool
    fire_after_losses: int
    lineup_size: int
    refresh_hours: float
    fire_cooldown_days: float
    only_first_buy: bool
    max_signal_age_seconds: float
    max_chase_pct: float
    rebuy_cooldown_hours: float
    follow_all: bool  # true: copy every trader on the list at once (top 1..lineup_size); false: follow_count at a time, fired in turn
    follow_count: int  # how many of the list are copied at once when follow_all is false (the Grok video: 2, "#1 and #2")
    window: str | None  # "22:00-08:00": new copies only inside this daily window (exits run all the time); null = always
    window_timezone: str  # "local" (the Mac's clock) or a name like Asia/Ho_Chi_Minh
    close_at_window_end: bool  # sell everything still open when the window closes


@dataclass(frozen=True)
class Exits:
    stop_loss_pct: float | None
    take_profit_pct: float | None
    max_hold_hours: float | None


@dataclass(frozen=True)
class Safety:
    min_liquidity_usd: float
    min_market_cap_usd: float
    max_market_cap_usd: float | None
    require_mint_authority_revoked: bool
    require_freeze_authority_revoked: bool
    blocked_token2022_extensions: list
    max_price_impact_pct: float
    check_sell_route: bool
    max_round_trip_loss_pct: float


@dataclass(frozen=True)
class Limits:
    max_open_positions: int
    max_position_pct: float
    max_exposure_pct: float
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    max_consecutive_api_errors: int
    min_sol_for_fees: float


@dataclass(frozen=True)
class Paper:
    slippage_bps: float


@dataclass(frozen=True)
class Watch:
    ws_connections: int  # websocket connections to the RPC (the free Helius plan allows 5)
    wallets_per_connection: int  # wallets subscribed on each one
    max_wallet_txs_per_hour: int  # a wallet busier than this is a bot (or too fast to copy) and is ignored for 6 hours
    max_tx_reads_per_hour: int  # transactions the bot reads per hour (each costs one RPC credit); trades of held coins come first


@dataclass(frozen=True)
class Notify:
    big_win_pct: float  # a copy that closes this much up gets its own "Big win" notification
    digest_minutes: float  # every this often: "Copied N trades · xW yL" for what closed meanwhile (0 = off)


@dataclass(frozen=True)
class GrokCfg:
    """xAI's Grok as the bot's brain: it puts the bench in order (who to copy next) and reads X about each coin
    we copy. It can only choose among the listed traders and only make us sell sooner, never buy."""
    enabled: bool
    model: str
    pick_traders: bool  # Grok orders the weekly top: who gets copied first, who replaces a fired trader
    pick_from: int  # how many of the top list Grok looks at
    x_check: bool  # after a copy fills, Grok searches X for the coin's contract address; a rug or scam warning sells it
    x_check_min_usd: float  # only copies at least this big get an X check (each costs about $0.10)
    daily_budget_usd: float  # Grok stops for the day after this much spend (the bot keeps copying by the rules)
    price_in_per_mtok: float
    price_out_per_mtok: float
    price_per_1k_x_posts: float  # X Search is billed per post read
    assumed_posts_per_search: int  # charged at least this many posts per X check, so the budget never undercounts
    timeout_seconds: float


@dataclass(frozen=True)
class FomoConfig:
    copy: CopyRules
    exits: Exits
    safety: Safety
    limits: Limits
    paper: Paper
    watch: Watch
    notify: Notify
    grok: GrokCfg


def _build(cls, data: dict, section: str):
    names = {f.name for f in fields(cls)}
    extra, missing = set(data) - names, names - set(data)
    if extra or missing:
        raise ValueError(f"config/fomo.yaml [{section}]: unknown {sorted(extra)} / missing {sorted(missing)}")
    return cls(**data)


def load_config(path: str | Path = "config/fomo.yaml") -> FomoConfig:
    d = yaml.safe_load(Path(path).read_text()) or {}
    c = FomoConfig(copy=_build(CopyRules, d.get("copy") or {}, "copy"), exits=_build(Exits, d.get("exits") or {}, "exits"),
                   safety=_build(Safety, d.get("safety") or {}, "safety"), limits=_build(Limits, d.get("limits") or {}, "limits"),
                   paper=_build(Paper, d.get("paper") or {}, "paper"), watch=_build(Watch, d.get("watch") or {}, "watch"),
                   notify=_build(Notify, d.get("notify") or {}, "notify"), grok=_build(GrokCfg, d.get("grok") or {}, "grok"))
    if not 0 < c.copy.copy_ratio <= 1:
        raise ValueError("copy_ratio must be between 0 and 1")
    if c.copy.min_trade_usd > c.copy.max_trade_usd:
        raise ValueError("min_trade_usd is above max_trade_usd")
    if c.copy.fire_after_losses < 1:
        raise ValueError("fire_after_losses must be at least 1")
    if c.copy.follow_count < 1:
        raise ValueError("follow_count must be at least 1")
    if c.copy.window is not None:
        window_minutes(c.copy.window)  # raises on a bad window
    window_zone(c.copy.window_timezone)
    room = c.watch.ws_connections * c.watch.wallets_per_connection
    if c.copy.follow_all and c.copy.lineup_size > room:
        raise ValueError(f"lineup_size {c.copy.lineup_size} is more than the websockets can watch "
                         f"({c.watch.ws_connections} x {c.watch.wallets_per_connection} = {room}): lower it or raise [watch]")
    return c


def window_minutes(window: str) -> tuple[int, int]:
    """"22:00-08:00" -> (1320, 480): minutes after midnight when it opens and when it closes."""
    try:
        a, b = (x.strip() for x in str(window).split("-"))
        (ah, am), (bh, bm) = (map(int, a.split(":")), map(int, b.split(":")))
        if not (0 <= ah < 24 and 0 <= bh < 24 and 0 <= am < 60 and 0 <= bm < 60) or (ah, am) == (bh, bm):
            raise ValueError
        return ah * 60 + am, bh * 60 + bm
    except ValueError:
        raise ValueError(f"copy.window '{window}' must look like 22:00-08:00") from None


def window_bounds(window: str, tz_name: str, now: float) -> tuple[float, float]:
    """(open, close) of the window `now` is in, or else of the next one, as unix times."""
    from datetime import datetime, timedelta
    open_m, close_m = window_minutes(window)
    length = ((close_m - open_m) % 1440) * 60
    tz = window_zone(tz_name)
    here = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    midnight = here.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [(midnight + timedelta(days=d, minutes=open_m)).timestamp() for d in (-1, 0, 1)]
    for s0 in starts:
        if s0 <= now < s0 + length:
            return s0, s0 + length
    nxt = min(s0 for s0 in starts if s0 > now)
    return nxt, nxt + length


def window_zone(name: str):
    """None for the Mac's own clock, else a ZoneInfo."""
    if name in ("", "local", None):
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"copy.window_timezone '{name}' is not a time zone (use local or a name like Asia/Ho_Chi_Minh)") from None
