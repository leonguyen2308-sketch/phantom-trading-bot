import base64

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from phantom.execution.jupiter import check_order, sign_order_transaction


def unsigned_tx_b64(payer):
    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(payer, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


def test_signs_in_the_takers_slot():
    kp = Keypair()
    signed = VersionedTransaction.from_bytes(base64.b64decode(sign_order_transaction(unsigned_tx_b64(kp.pubkey()), kp)))
    assert signed.signatures[0].verify(kp.pubkey(), to_bytes_versioned(signed.message))


def test_refuses_transaction_that_does_not_need_our_signature():
    with pytest.raises(ValueError):
        sign_order_transaction(unsigned_tx_b64(Keypair().pubkey()), Keypair())


def test_order_must_match_what_we_asked_for():
    order = {"inputMint": "A", "outputMint": "B", "inAmount": "1000", "outAmount": "5", "transaction": "x", "requestId": "r"}
    check_order(order, input_mint="A", output_mint="B", amount=1000)
    with pytest.raises(ValueError):
        check_order({**order, "outputMint": "EVIL"}, input_mint="A", output_mint="B", amount=1000)
    with pytest.raises(ValueError):
        check_order({**order, "inAmount": "999999"}, input_mint="A", output_mint="B", amount=1000)
    with pytest.raises(ValueError):
        check_order({**order, "transaction": None}, input_mint="A", output_mint="B", amount=1000)


# ---- review fixes: transaction inspection, unknown outcomes, fee-aware fills ----
import httpx  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from phantom.execution.base import Prepared  # noqa: E402
from phantom.execution.jupiter import JupiterBroker, SwapOutcomeUnknown, impact_bps  # noqa: E402

TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")


def tx_b64(payer, ixs):
    msg = MessageV0.try_compile(payer, ixs, [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


def test_refuses_token_approval():
    kp = Keypair()
    acct, attacker = Keypair().pubkey(), Keypair().pubkey()
    approve = Instruction(TOKEN, bytes([4]) + (2**64 - 1).to_bytes(8, "little"),
                          [AccountMeta(acct, False, True), AccountMeta(attacker, False, False), AccountMeta(kp.pubkey(), True, False)])
    with pytest.raises(ValueError, match="approve"):
        sign_order_transaction(tx_b64(kp.pubkey(), [approve]), kp)


def test_refuses_draining_sol():
    kp = Keypair()
    big = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1_000_000_000))
    with pytest.raises(ValueError, match="SOL"):
        sign_order_transaction(tx_b64(kp.pubkey(), [big]), kp)


def test_price_impact_fails_closed_and_prefers_documented_field():
    assert impact_bps({}) == 1e9
    assert impact_bps({"priceImpact": -1.2}) == pytest.approx(120)
    assert impact_bps({"priceImpactPct": "0.0012"}) == pytest.approx(12)


def _broker(handler, kp):
    return JupiterBroker(kp, "k", "https://rpc.example", http=httpx.Client(transport=httpx.MockTransport(handler)),
                         poll_seconds=0.05, sleep=lambda s: None)


def _prepared(kp):
    order = {"transaction": unsigned_tx_b64(kp.pubkey()), "requestId": "r", "outAmount": "100000"}
    return Prepared("buy", 100.0, 0.001, 100_000.0, 1.0, order)


def test_execute_keeps_polling_after_a_timeout_then_uses_wallet_amount():
    kp, calls = Keypair(), []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"status": "Success", "signature": "sig", "totalOutputAmount": "99000", "outputAmountResult": "100000"})

    fill = _broker(handler, kp).execute(_prepared(kp))
    assert len(calls) == 2 and fill.qty == pytest.approx(0.00099)  # what reached the wallet, after fees


def test_execute_reports_unknown_not_failed_when_it_never_resolves():
    kp = Keypair()
    b = _broker(lambda r: httpx.Response(500, json={"signature": "s", "error": "x"}), kp)
    with pytest.raises(SwapOutcomeUnknown):
        b.execute(_prepared(kp))
