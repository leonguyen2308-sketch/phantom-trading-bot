"""Live swaps on Solana through Jupiter Swap API v2 (order -> sign -> execute), USDC <-> cbBTC.

The bot signs with its own wallet's key. Before signing, it checks that Jupiter's order is for
exactly the mints and amount it asked for, and that the transaction actually needs the bot's
signature. Nothing else in the wallet is touched: sells are for the bot's tracked quantity only.
"""

from __future__ import annotations

import base64
import logging

import httpx
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from phantom.execution.base import Fill, Prepared

log = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CBBTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"
DECIMALS = {USDC_MINT: 6, CBBTC_MINT: 8}
DEFAULT_BASE = "https://api.jup.ag/swap/v2"


def check_order(order: dict, *, input_mint: str, output_mint: str, amount: int) -> None:
    """Refuse an order that isn't exactly what we requested."""
    if not order.get("transaction"):
        raise ValueError(f"Jupiter returned no transaction: {order.get('error') or order.get('errorMessage') or 'unknown'}")
    if not order.get("requestId"):
        raise ValueError("Jupiter order has no requestId")
    if order.get("inputMint") != input_mint or order.get("outputMint") != output_mint:
        raise ValueError("Jupiter order mints do not match the request")
    if int(order.get("inAmount", -1)) != int(amount):
        raise ValueError("Jupiter order input amount does not match the request")
    if int(order.get("outAmount", 0)) <= 0:
        raise ValueError("Jupiter order has no output amount")


def sign_order_transaction(tx_b64: str, kp: Keypair) -> str:
    """Add our signature in our own signer slot; leave any other signer (e.g. a market maker) to Jupiter."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    n_signers = msg.header.num_required_signatures
    keys = list(msg.account_keys)[:n_signers]
    if kp.pubkey() not in keys:
        raise ValueError("This transaction does not ask for the bot wallet's signature")
    sigs = list(tx.signatures)
    sigs[keys.index(kp.pubkey())] = kp.sign_message(to_bytes_versioned(msg))
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


class JupiterBroker:
    mode = "live"

    def __init__(self, kp: Keypair, api_key: str, rpc_url: str, base_url: str = DEFAULT_BASE, http: httpx.Client | None = None):
        self.kp, self.base, self.rpc_url = kp, base_url.rstrip("/"), rpc_url
        self.http = http or httpx.Client(timeout=20)
        self.headers = {"x-api-key": api_key}

    # ---- RPC ----
    def _rpc(self, method: str, params: list) -> dict:
        r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"Solana RPC {method} failed: {body['error']}")
        return body["result"]

    def _token_balance(self, mint: str) -> float:
        res = self._rpc("getTokenAccountsByOwner", [str(self.kp.pubkey()), {"mint": mint}, {"encoding": "jsonParsed"}])
        raw = sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])
        return raw / 10 ** DECIMALS[mint]

    def verify_mints(self) -> None:
        """Startup check: the mint addresses really are 6-decimal USDC and 8-decimal cbBTC."""
        for mint, dec in DECIMALS.items():
            got = self._rpc("getTokenSupply", [mint])["value"]["decimals"]
            if got != dec:
                raise RuntimeError(f"Mint {mint} has {got} decimals, expected {dec}; refusing to trade")

    def balances(self) -> dict[str, float]:
        lamports = self._rpc("getBalance", [str(self.kp.pubkey())])["value"]
        return {"usd": self._token_balance(USDC_MINT), "btc": self._token_balance(CBBTC_MINT), "sol": lamports / 1e9}

    # ---- swaps ----
    def _order(self, input_mint: str, output_mint: str, amount: int) -> dict:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "taker": str(self.kp.pubkey())}
        r = self.http.get(f"{self.base}/order", params=params, headers=self.headers)
        r.raise_for_status()
        order = r.json()
        check_order(order, input_mint=input_mint, output_mint=output_mint, amount=amount)
        return order

    @staticmethod
    def _impact_bps(order: dict) -> float:
        # priceImpactPct is a fraction (e.g. "0.0012" = 12 bps). If the API ever reports percent
        # instead, this overstates impact and the risk check refuses the trade: the safe failure.
        try:
            return abs(float(order.get("priceImpactPct", 0))) * 1e4
        except (TypeError, ValueError):
            return 1e9

    def prepare_buy(self, usd: float, market_price: float) -> Prepared:
        amount = int(usd * 10 ** 6)
        o = self._order(USDC_MINT, CBBTC_MINT, amount)
        btc = int(o["outAmount"]) / 10 ** 8
        return Prepared("buy", usd, btc, usd / btc, self._impact_bps(o), o)

    def prepare_sell(self, btc: float, market_price: float) -> Prepared:
        amount = int(btc * 10 ** 8)
        o = self._order(CBBTC_MINT, USDC_MINT, amount)
        usd = int(o["outAmount"]) / 10 ** 6
        return Prepared("sell", amount / 10 ** 8, usd, usd / (amount / 10 ** 8), self._impact_bps(o), o)

    def execute(self, p: Prepared) -> Fill:
        o = p.payload
        signed = sign_order_transaction(o["transaction"], self.kp)
        r = self.http.post(f"{self.base}/execute", json={"signedTransaction": signed, "requestId": o["requestId"]}, headers=self.headers)
        r.raise_for_status()
        res = r.json()
        if str(res.get("status", "")).lower() != "success":
            raise RuntimeError(f"Swap failed: {res.get('error') or res.get('status')} (code {res.get('code')})")
        sig = res.get("signature", "")
        if p.side == "buy":
            out_btc = int(res.get("outputAmountResult") or o["outAmount"]) / 10 ** 8
            return Fill("buy", out_btc, p.in_amount / out_btc, p.in_amount, sig)
        out_usd = int(res.get("outputAmountResult") or o["outAmount"]) / 10 ** 6
        return Fill("sell", p.in_amount, out_usd / p.in_amount, out_usd, sig)


def is_valid_address(s: str) -> bool:
    try:
        Pubkey.from_string(s)
        return True
    except Exception:
        return False
