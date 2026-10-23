"""Crowd mode: the shipped config copies every trader on the list at once."""

import dataclasses

import pytest

from fomo.bot import CopyBot
from fomo.chain import MintInfo
from fomo.config import load_config
from fomo.journal import Journal
from fomo.leaderboard import Trader
from phantom.risk import KillSwitch
from tests.test_fomo_bot import Broker, Clock, Dex, FakeWatcher, Market, mint, run, trade


def wallet(i):
    return f"W{i:03d}".ljust(44, "w")


class Watcher(FakeWatcher):
    priority: set = set()


class Alerts:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def make_world(tmp_path, n=30, cfg=None):
    clock, m, w = Clock(), Market(), Watcher()
    cfg = cfg or load_config("config/fomo.yaml")
    bot = CopyBot(mode="paper", cfg=cfg, broker=Broker(m), watcher=w, prices=m, dex=Dex(m),
                  mint_info=lambda mt: MintInfo(mt, 6, None, "F" * 44 if mt in m.freeze else None, "spl-token"),
                  lineup=lambda: [Trader(f"t{i}", wallet(i), i) for i in range(1, n + 1)],
                  journal=Journal(tmp_path / "j.sqlite"), kill=KillSwitch(tmp_path / "k.json"), clock=clock, alerts=Alerts())
    bot.step()
    return bot, m, w, clock


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def test_the_shipped_config_is_crowd_mode_with_room_for_five_hundred():
    cfg = load_config("config/fomo.yaml")
    assert cfg.copy.follow_all and cfg.copy.lineup_size == 500
    assert cfg.watch.ws_connections * cfg.watch.wallets_per_connection >= cfg.copy.lineup_size


def test_a_list_longer_than_the_websockets_can_carry_is_refused(tmp_path):
    text = open("config/fomo.yaml").read().replace("lineup_size: 500", "lineup_size: 501")
    f = tmp_path / "fomo.yaml"
    f.write_text(text)
    with pytest.raises(ValueError, match="more than the websockets can watch"):
        load_config(f)


def test_every_trader_on_the_list_is_watched_and_copied_not_only_the_first(world):
    bot, m, w, clock = world
    assert w.watched == {wallet(i) for i in range(1, 31)}
    for i, rank in enumerate((1, 17, 30), 1):
        m.px[mint(i)] = 0.01
        run(bot, w, clock, trade(wallet(rank), "buy", mint(i), 40, 0.01, clock.t))
    assert sorted(c["trader"] for c in bot.journal.open_copies("paper")) == sorted([wallet(1), wallet(17), wallet(30)])
    assert [round(c["cost_usd"], 2) for c in bot.journal.open_copies("paper")] == [5.0, 5.0, 5.0]  # 40 x 0.25 = 10, capped at $5


def test_a_wallet_that_is_not_on_the_list_is_not_copied(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    w.watched.add(wallet(99))
    run(bot, w, clock, trade(wallet(99), "buy", mint(1), 40, 0.01, clock.t))
    assert bot.journal.open_copies("paper") == []


def test_a_second_trader_buying_what_we_hold_is_not_added_on_top(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(wallet(1), "buy", mint(1), 40, 0.01, clock.t))
    run(bot, w, clock, trade(wallet(2), "buy", mint(1), 40, 0.01, clock.t))
    (c,) = bot.journal.open_copies("paper")
    assert c["trader"] == wallet(1) and c["cost_usd"] == pytest.approx(5.0)
    assert "another trader" in bot.journal.events("paper")[0]["text"]


def lose(bot, m, w, clock, who, i):
    m.px[mint(i)] = 0.01
    b = trade(wallet(who), "buy", mint(i), 40, 0.01, clock.t)
    run(bot, w, clock, b)
    m.px[mint(i)] = 0.008
    run(bot, w, clock, trade(wallet(who), "sell", mint(i), 30, 0.008, clock.t, pre=b.raw))


def test_one_trader_is_fired_after_three_losses_and_everyone_else_is_still_copied(world):
    bot, m, w, clock = world
    for i in (1, 2):
        lose(bot, m, w, clock, 5, i)
    assert not bot.rot.is_fired(wallet(5), clock.t)
    lose(bot, m, w, clock, 5, 3)
    assert bot.rot.is_fired(wallet(5), clock.t)
    assert "FIRE" in [e["kind"] for e in bot.journal.events("paper")]
    m.px[mint(10)] = m.px[mint(11)] = 0.01
    run(bot, w, clock, trade(wallet(5), "buy", mint(10), 40, 0.01, clock.t), trade(wallet(6), "buy", mint(11), 40, 0.01, clock.t))
    assert [c["trader"] for c in bot.journal.open_copies("paper")] == [wallet(6)]
    assert bot.rot.active(clock.t)[0].wallet == wallet(1) and len(bot.rot.active(clock.t)) == 29


def test_a_fired_trader_is_still_watched_while_we_hold_their_coin_and_it_exits_when_they_sell(world):
    bot, m, w, clock = world
    m.px[mint(9)] = 0.01
    keep = trade(wallet(5), "buy", mint(9), 40, 0.01, clock.t)
    run(bot, w, clock, keep)
    for i in (1, 2, 3):
        lose(bot, m, w, clock, 5, i)
    assert bot.rot.is_fired(wallet(5), clock.t)
    assert wallet(5) in w.watched and w.priority == {wallet(5)}
    run(bot, w, clock, trade(wallet(5), "sell", mint(9), 40, 0.01, clock.t, pre=keep.raw))
    assert bot.journal.open_copies("paper") == []


def test_the_exposure_and_open_position_limits_hold_for_the_whole_crowd(world, tmp_path):
    bot, m, w, clock = world
    for i in range(1, 22):
        m.px[mint(i)] = 0.01
        run(bot, w, clock, trade(wallet(i), "buy", mint(i), 40, 0.01, clock.t))
    assert len(bot.journal.open_copies("paper")) == 12  # $5 each: 60% of $100 is spent, so the 13th is refused
    cfg = load_config("config/fomo.yaml")
    cfg = dataclasses.replace(cfg, limits=dataclasses.replace(cfg.limits, max_open_positions=4))
    bot, m, w, clock = make_world(tmp_path / "few", cfg=cfg)
    for i in range(1, 10):
        m.px[mint(i)] = 0.01
        run(bot, w, clock, trade(wallet(i), "buy", mint(i), 40, 0.01, clock.t))
    assert len(bot.journal.open_copies("paper")) == 4


def test_skips_are_counted_by_reason_and_the_feed_stays_readable(world):
    bot, m, w, clock = world
    for i in range(1, 11):
        m.px[mint(i)] = 0.01
        m.liq[mint(i)] = 1000 + i  # ten different thin pools: ten different numbers in the reason
        bot.clock.t += 0  # the same second
        run(bot, w, clock, trade(wallet(i), "buy", mint(i), 40, 0.01, clock.t), advance=0)
    skips = [e for e in bot.journal.events("paper", 50) if e["kind"] == "SKIP"]
    assert len(skips) == 1  # one line every 20 seconds
    s = bot.dashboard_state()["crowd"]["skips"]
    assert s[0]["n"] == 10 and "liquidity" in s[0]["reason"] and "1001" not in s[0]["reason"]


def test_a_coin_refused_for_what_it_is_is_not_looked_up_again_for_a_while(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    m.freeze.add(mint(1))
    lookups = []
    real = bot.dex.get
    bot.dex.get = lambda mt: lookups.append(mt) or real(mt)
    for who in (1, 2, 3):
        run(bot, w, clock, trade(wallet(who), "buy", mint(1), 40, 0.01, clock.t))
    assert len(lookups) == 1 and bot.journal.open_copies("paper") == []


def test_one_coin_bought_by_many_traders_is_read_from_the_market_once(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    lookups = []
    real = bot.dex.get
    bot.dex.get = lambda mt: lookups.append(mt) or real(mt)
    for who in (1, 2, 3):
        run(bot, w, clock, trade(wallet(who), "buy", mint(1), 40, 0.01, clock.t))
    assert len(lookups) == 1


def test_the_dashboard_state_summarises_the_crowd_and_trims_the_table(world, tmp_path):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(wallet(3), "buy", mint(1), 40, 0.01, clock.t), advance=20)
    s = bot.dashboard_state()
    assert s["following"] is None and s["crowd"]["on"] and s["crowd"]["listed"] == 30 and s["crowd"]["followed"] == 30
    assert s["crowd"]["signals1h"] == 1 and s["crowd"]["copies1h"] == 1 and len(s["lineup"]) == 30
    big = make_world(tmp_path / "big", n=300)[0].dashboard_state()
    assert len(big["lineup"]) == 40 and big["crowd"]["listed"] == 300 and big["lineup"][0]["rank"] == 1


def test_telegram_only_hears_about_what_needs_you_in_crowd_mode(world):
    bot, m, w, clock = world
    m.px[mint(1)] = 0.01
    run(bot, w, clock, trade(wallet(1), "buy", mint(1), 40, 0.01, clock.t))
    assert bot.alerts.sent == []  # a copy is not worth a phone buzz when there are hundreds
    bot.trip("test")
    assert any("test" in t for t in bot.alerts.sent)


def test_follow_all_false_is_the_old_one_at_a_time_rule(tmp_path):
    cfg = load_config("tests/fomo_classic.yaml")
    bot, m, w, clock = make_world(tmp_path, cfg=cfg)
    assert w.watched == {wallet(1)}
    assert dataclasses.is_dataclass(cfg) and not cfg.copy.follow_all
