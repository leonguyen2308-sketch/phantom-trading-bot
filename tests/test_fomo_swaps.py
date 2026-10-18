import pytest

from fomo.swaps import USDC, WSOL, parse_swap

W = "Wa11et1111111111111111111111111111111111111"
FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
MEME = "MemeMint11111111111111111111111111111111111"
OTHER = "OtherMint1111111111111111111111111111111111"


def tb(i, mint, owner, amount, decimals):
    return {"accountIndex": i, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def tx(pre_tok, post_tok, keys=(W,), pre_sol=None, post_sol=None, fee=5000, err=None):
    n = len(keys)
    return {"slot": 10, "blockTime": 1_700_000_000,
            "transaction": {"signatures": ["sig1"], "message": {"accountKeys": [{"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)]}},
            "meta": {"err": err, "fee": fee, "preBalances": list(pre_sol or [10**9] * n), "postBalances": list(post_sol or [10**9] * n),
                     "preTokenBalances": pre_tok, "postTokenBalances": post_tok}}


def test_usdc_buy():
    t = tx([tb(1, USDC, W, 500_000_000, 6)], [tb(1, USDC, W, 400_000_000, 6), tb(2, MEME, W, 1_000_000_000, 6)])
    s = parse_swap(t, W, sol_usd=150)
    assert s.side == "buy" and s.mint == MEME and s.usd == pytest.approx(100) and s.quote == "USDC"
    assert s.amount == pytest.approx(1000) and s.price == pytest.approx(0.1) and s.pre_raw == 0


def test_partial_sell_reports_the_share_sold():
    t = tx([tb(1, USDC, W, 0, 6), tb(2, MEME, W, 1000, 0)], [tb(1, USDC, W, 60_000_000, 6), tb(2, MEME, W, 400, 0)])
    s = parse_swap(t, W, 150)
    assert s.side == "sell" and s.usd == pytest.approx(60) and s.sold_fraction == pytest.approx(0.6)


def test_sol_buy_ignores_the_network_fee_and_counts_wrapped_sol():
    # paid 0.5 SOL native + 0.5 SOL through a wrapped-SOL account, plus a 5000-lamport fee
    t = tx([tb(1, WSOL, W, 500_000_000, 9)], [tb(1, WSOL, W, 0, 9), tb(2, MEME, W, 777, 0)],
           pre_sol=[2 * 10**9], post_sol=[2 * 10**9 - 500_000_000 - 5000])
    s = parse_swap(t, W, sol_usd=200)
    assert s.side == "buy" and s.quote == "SOL" and s.usd == pytest.approx(200.0)


def test_fomo_style_trade_where_the_app_pays_the_fee():
    # FOMO's signer pays gas at index 0; the user's wallet only owns the token accounts
    t = tx([tb(2, USDC, W, 100_000_000, 6)], [tb(2, USDC, W, 49_500_000, 6), tb(3, MEME, W, 5_000, 2)],
           keys=(FOMO_SIGNER, W), pre_sol=[10**9, 10**8], post_sol=[10**9 - 9000, 10**8])
    s = parse_swap(t, W, 150)
    assert s.side == "buy" and s.usd == pytest.approx(50.5) and s.amount == pytest.approx(50)


def test_not_a_trade():
    airdrop = tx([], [tb(2, MEME, W, 1000, 0)])
    assert parse_swap(airdrop, W, 150) is None
    failed = tx([tb(1, USDC, W, 10**8, 6)], [tb(1, USDC, W, 0, 6), tb(2, MEME, W, 1, 0)], err={"InstructionError": [0, 1]})
    assert parse_swap(failed, W, 150) is None
    token_for_token = tx([tb(2, MEME, W, 10, 0)], [tb(2, MEME, W, 0, 0), tb(3, OTHER, W, 5, 0)])
    assert parse_swap(token_for_token, W, 150) is None
    someone_else = tx([tb(1, USDC, "x" * 43, 10**8, 6)], [tb(1, USDC, "x" * 43, 0, 6), tb(2, MEME, "x" * 43, 1, 0)])
    assert parse_swap(someone_else, W, 150) is None
    assert parse_swap(None, W, 150) is None


def test_dust_is_ignored():
    t = tx([tb(1, USDC, W, 100_000, 6)], [tb(1, USDC, W, 0, 6), tb(2, MEME, W, 5, 0)])  # $0.10
    assert parse_swap(t, W, 150) is None
