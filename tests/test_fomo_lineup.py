"""Where the top 1..500 traders come from."""

import httpx
import pytest
from solders.keypair import Keypair

from fomo.leaderboard import from_solanatracker, from_wallet_list, load_lineup, parse_solanatracker


def w():
    return str(Keypair().pubkey())


def test_a_wallet_list_is_one_address_per_line_with_optional_names_and_comments(tmp_path):
    a, b, c = w(), w(), w()
    f = tmp_path / "wallets.txt"
    f.write_text(f"# my list\n{a}\n\n{b} whale_one\nsome_name, {c}  # trailing note\n")
    got = from_wallet_list(f)
    assert [(t.rank, t.handle, t.wallet) for t in got] == [(1, a[:6], a), (2, "whale_one", b), (3, "some_name", c)]


def test_a_missing_wallet_list_is_just_empty_and_a_bad_line_says_which(tmp_path):
    assert from_wallet_list(tmp_path / "nope.txt") == []
    f = tmp_path / "wallets.txt"
    f.write_text(f"{w()}\nnot-an-address\n")
    with pytest.raises(ValueError, match="line 2"):
        from_wallet_list(f)


def test_the_solana_tracker_leaderboard_is_parsed_whatever_it_calls_the_fields():
    a, b, c = w(), w(), w()
    body = {"traders": [
        {"wallet": a, "period": {"realized": 12345.6, "winRate": 0.61}, "identity": {"name": "@ansem"}},
        {"address": b, "realized": 900.0},
        {"wallet": "not a wallet"},
        {"wallet": a},  # a repeat
        {"owner": c, "pnl": -5},
    ]}
    got = parse_solanatracker(body)
    assert [(t.rank, t.handle, t.wallet) for t in got] == [(1, "ansem", a), (2, b[:6], b), (3, c[:6], c)]
    assert got[0].pnl_7d == pytest.approx(12345.6) and got[0].win_rate == pytest.approx(0.61) and got[0].source == "solanatracker"
    assert parse_solanatracker({"unexpected": 1}) == [] and parse_solanatracker([]) == []


def test_it_asks_for_everything_and_walks_pages_if_the_api_caps_one():
    wallets = [w() for _ in range(250)]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        calls.append((page, request.url.params["limit"], request.headers["x-api-key"]))
        rows = wallets[(page - 1) * 100: page * 100]  # this API serves 100 a page whatever the limit
        return httpx.Response(200, json={"traders": [{"wallet": x, "realized": 1.0} for x in rows]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    got = from_solanatracker("KEY123", 250, http=http)
    assert [t.wallet for t in got] == wallets and [t.rank for t in got] == list(range(1, 251))
    assert calls[0] == (1, "250", "KEY123") and len(calls) == 3


def test_it_stops_when_a_page_adds_nothing_new():
    a = w()
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"traders": [{"wallet": a}]})))  # ignores paging
    assert [t.wallet for t in from_solanatracker("K", 500, http=http)] == [a]


def test_the_lineup_falls_back_from_solana_tracker_to_your_files(tmp_path):
    a, b = w(), w()
    wallets = tmp_path / "wallets.txt"
    wallets.write_text(f"{a}\n")
    yaml = tmp_path / "traders.yaml"
    yaml.write_text(f"traders:\n  - handle: first\n    wallet: {b}\n")
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"error": "plan"})))
    got = load_lineup(10, "", yaml, refused, tracker_key="K", wallets_path=wallets)
    assert [(t.rank, t.handle, t.wallet) for t in got] == [(1, "first", b), (2, a[:6], a)]  # traders.yaml first, then wallets.txt


def test_solana_tracker_wins_when_it_works_and_the_list_is_cut_to_size(tmp_path):
    wallets = [w() for _ in range(8)]
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"traders": [{"wallet": x} for x in wallets]})))
    got = load_lineup(5, "", tmp_path / "none.yaml", http, tracker_key="K", wallets_path=tmp_path / "none.txt")
    assert [t.wallet for t in got] == wallets[:5]
