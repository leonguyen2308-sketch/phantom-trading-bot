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
