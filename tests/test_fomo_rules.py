from fomo.leaderboard import Trader, parse_fomoapi
from fomo.rules import Rotation

A, B, C = "A" * 44, "B" * 44, "C" * 44


def lineup():
    return [Trader("ace", A, 1), Trader("bee", B, 2), Trader("cat", C, 3)]


def test_follows_the_top_trader_first():
    r = Rotation(2, 7 * 86400)
    events = r.set_lineup(lineup(), now=0)
    assert r.following.handle == "ace"
    assert any("follow" in e for e in events)


def test_two_losses_in_a_row_fire_and_move_to_the_next():
    r = Rotation(2, 7 * 86400)
    r.set_lineup(lineup(), 0)
    assert r.record(A, -1.0, 10) == (False, [])
    fired, events = r.record(A, -0.5, 20)
    assert fired and r.following.handle == "bee"
    assert any("fired" in e for e in events)
    assert r.s["stints"][0]["fired"] and r.s["stints"][0]["end"] == 20
    assert r.stats(A)["last"] == ["L", "L"]


def test_a_win_resets_the_streak():
    r = Rotation(2, 7 * 86400)
    r.set_lineup(lineup(), 0)
    r.record(A, -1, 1)
    r.record(A, +3, 2)
    fired, _ = r.record(A, -1, 3)
    assert not fired and r.following.handle == "ace"


def test_fired_trader_stays_out_until_the_cooldown_ends():
    r = Rotation(1, 100)
    r.set_lineup(lineup(), 0)
    r.record(A, -1, 10)
    r.set_lineup(lineup(), 50)  # weekly refresh: ace is still fired
    assert r.following.handle == "bee"
    assert r.is_fired(A, 50) and not r.is_fired(A, 111)


def test_everyone_fired_means_copy_nobody():
    r = Rotation(1, 1000)
    r.set_lineup(lineup(), 0)
    for w in (A, B, C):
        r.record(w, -1, 1)
    assert r.following is None


def test_late_loss_from_an_old_trader_fires_them_without_moving_the_current_one():
    r = Rotation(1, 1000)
    r.set_lineup(lineup(), 0)
    r.record(A, -1, 1)  # ace fired -> bee
    fired, _ = r.record(C, -1, 2)  # cat had an old copy close at a loss
    assert fired and r.following.handle == "bee"


def test_state_survives_a_restart():
    r = Rotation(2, 1000)
    r.set_lineup(lineup(), 0)
    r.record(A, -1, 1)
    again = Rotation(2, 1000, r.s)
    fired, _ = again.record(A, -1, 2)
    assert fired and again.following.handle == "bee"


def test_fomoapi_shapes():
    w = "7VRD4eLZVU8qGwTUjfCRTCvkMTzAiTbSZ8CzU9zWBDU6"
    a = parse_fomoapi({"leaderboard": [{"handle": "@kai", "pnlUsd": 1200, "wallets": {"solana": w, "evm": "0x1"}},
                                       {"handle": "noevm", "wallets": {"solana": None}}]})
    assert [(t.handle, t.wallet, t.pnl_7d, t.rank) for t in a] == [("kai", w, 1200.0, 1)]
    b = parse_fomoapi([{"user_id": 1, "handle": "x", "pnl_usd": "55.5", "wallets": {"solana": w}}])
    assert b[0].pnl_7d == 55.5
    c = parse_fomoapi({"responseObject": {"leaderboard": [{"userHandle": "y", "pnl": {"7d": 9}, "walletAddress": w}]}})
    assert c[0].handle == "y" and c[0].pnl_7d == 9.0
    assert parse_fomoapi({"leaderboard": [{"handle": "bad", "wallet": "not-a-wallet"}]}) == []
