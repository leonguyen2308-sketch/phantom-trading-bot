"""Live swaps on Solana through Jupiter Swap API v2 (order -> sign -> execute), USDC <-> cbBTC.

The bot signs with its own wallet's key. Before signing, it checks that Jupiter's order is for
exactly the mints and amount it asked for, and inspects the transaction itself: it refuses token
approvals, authority changes, closing accounts to anyone else, and SOL leaving the wallet beyond a
small fee allowance. After sending, it keeps polling until the swap is known to have landed or
not; "unknown" is never treated as "failed".
"""

from __future__ import annotations

import base64
import logging
import struct
import time

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

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAMS = {Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                  Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")}
TOKEN_APPROVE, TOKEN_SET_AUTHORITY, TOKEN_CLOSE, TOKEN_APPROVE_CHECKED = 4, 6, 9, 13
SYS_CREATE, SYS_ASSIGN, SYS_TRANSFER, SYS_CREATE_SEED = 0, 1, 2, 3
MAX_SOL_OUT_LAMPORTS = 20_000_000  # 0.02 SOL: account rent + tips; anything more is refused

# /execute codes that mean "not known yet": keep re-submitting the same signed transaction to poll.
UNKNOWN_CODES = {-1000, -1001, -1006, -2000, -2001}
POLL_SECONDS = 110  # Jupiter accepts re-submission of the same requestId for about 2 minutes


class SwapOutcomeUnknown(RuntimeError):
    """The swap may or may not have landed. The bot must reconcile with the wallet balance."""


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


def check_transaction(tx: VersionedTransaction, bot: Pubkey) -> None:
    """Refuse instructions that could hand control of the wallet or its funds to someone else."""
    msg = tx.message
    static = list(msg.account_keys)
    sol_out = 0

    def key(ix, i):
        idx = ix.accounts[i]
        return static[idx] if idx < len(static) else None  # lookup-table keys are never the signer

    for ix in msg.instructions:
        program = static[ix.program_id_index]
        data = bytes(ix.data)
        if program in TOKEN_PROGRAMS and data:
            kind = data[0]
            if kind in (TOKEN_APPROVE, TOKEN_APPROVE_CHECKED):
                raise ValueError("transaction tries to approve a delegate on a token account")
            if kind == TOKEN_SET_AUTHORITY:
                raise ValueError("transaction tries to change a token account's authority")
            if kind == TOKEN_CLOSE and len(ix.accounts) > 1 and key(ix, 1) != bot:
                raise ValueError("transaction closes a token account and sends its rent to someone else")
        if program == SYSTEM_PROGRAM and len(data) >= 4:
            kind = struct.unpack_from("<I", data)[0]
            if kind == SYS_ASSIGN and ix.accounts and key(ix, 0) == bot:
                raise ValueError("transaction tries to reassign the bot wallet")
            if kind in (SYS_CREATE, SYS_TRANSFER, SYS_CREATE_SEED) and ix.accounts and key(ix, 0) == bot:
                offset = {SYS_CREATE: 4, SYS_TRANSFER: 4, SYS_CREATE_SEED: None}[kind]
                if offset is None:  # create-with-seed: lamports follow base pubkey + seed
                    seed_len = struct.unpack_from("<Q", data, 36)[0]
                    offset = 44 + seed_len
                sol_out += struct.unpack_from("<Q", data, offset)[0]
    if sol_out > MAX_SOL_OUT_LAMPORTS:
        raise ValueError(f"transaction would move {sol_out / 1e9:.4f} SOL out of the bot wallet")


def sign_order_transaction(tx_b64: str, kp: Keypair) -> str:
    """Inspect, then add our signature in our own signer slot; other signers are left to Jupiter."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    n_signers = msg.header.num_required_signatures
    keys = list(msg.account_keys)[:n_signers]
    if kp.pubkey() not in keys:
        raise ValueError("This transaction does not ask for the bot wallet's signature")
    check_transaction(tx, kp.pubkey())
    sigs = list(tx.signatures)
    sigs[keys.index(kp.pubkey())] = kp.sign_message(to_bytes_versioned(msg))
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


def impact_bps(order: dict) -> float:
    """Price impact in bps. Prefers `priceImpact` (percent); missing or unreadable fails closed."""
    try:
        if order.get("priceImpact") is not None:
            return abs(float(order["priceImpact"])) * 100
        if order.get("priceImpactPct") is not None:  # deprecated: a fraction
            return abs(float(order["priceImpactPct"])) * 1e4
    except (TypeError, ValueError):
        pass
    return 1e9


class JupiterBroker:
    mode = "live"

    def __init__(self, kp: Keypair, api_key: str, rpc_url: str, base_url: str = DEFAULT_BASE,
                 http: httpx.Client | None = None, poll_seconds: float = POLL_SECONDS, sleep=time.sleep):
        self.kp, self.base, self.rpc_url = kp, base_url.rstrip("/"), rpc_url
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"x-api-key": api_key}
        self.poll_seconds, self.sleep = poll_seconds, sleep

    # ---- RPC ----
    def _rpc(self, method: str, params: list) -> dict:
        try:
            r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as e:  # never let the RPC URL (it carries an API key) reach a log
            raise RuntimeError(f"Solana RPC {method} failed: {type(e).__name__}") from None
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

    def prepare_buy(self, usd: float, market_price: float) -> Prepared:
        amount = int(usd * 10 ** 6)
        o = self._order(USDC_MINT, CBBTC_MINT, amount)
        btc = int(o["outAmount"]) / 10 ** 8
        floor = int(o.get("otherAmountThreshold") or 0) / 10 ** 8
        worst = (amount / 10 ** 6) / floor if floor > 0 else float("inf")
        return Prepared("buy", amount / 10 ** 6, btc, (amount / 10 ** 6) / btc, impact_bps(o), o, worst_price=worst)

    def prepare_sell(self, btc: float, market_price: float) -> Prepared:
        amount = int(btc * 10 ** 8)  # floor: never ask to sell more than we hold
        if amount <= 0:
            raise ValueError("nothing to sell")
        o = self._order(CBBTC_MINT, USDC_MINT, amount)
        usd = int(o["outAmount"]) / 10 ** 6
        floor = int(o.get("otherAmountThreshold") or 0) / 10 ** 6
        qty = amount / 10 ** 8
        return Prepared("sell", qty, usd, usd / qty, impact_bps(o), o, worst_price=floor / qty if floor else 0.0)

    def _fill(self, p: Prepared, res: dict) -> Fill:
        o, sig = p.payload, res.get("signature", "")
        out_raw = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or o["outAmount"])
        in_raw = res.get("totalInputAmount") or res.get("inputAmountResult")
        if p.side == "buy":
            usd = int(in_raw) / 10 ** 6 if in_raw else p.in_amount
            qty = out_raw / 10 ** 8
            return Fill("buy", qty, usd / qty, usd, sig)
        qty = int(in_raw) / 10 ** 8 if in_raw else p.in_amount
        usd = out_raw / 10 ** 6
        return Fill("sell", qty, usd / qty, usd, sig)

    def execute(self, p: Prepared) -> Fill:
        o = p.payload
        body = {"signedTransaction": sign_order_transaction(o["transaction"], self.kp), "requestId": o["requestId"]}
        deadline = time.monotonic() + self.poll_seconds
        last = "no response"
        while True:
            try:
                r = self.http.post(f"{self.base}/execute", json=body, headers=self.headers)
                if r.status_code < 500:
                    r.raise_for_status()  # a 4xx means the request was refused: nothing was sent on-chain
                res = r.json() if r.content else {}
                status = str(res.get("status", "")).lower()
                if status == "success":
                    return self._fill(p, res)
                code = res.get("code")
                if r.status_code < 500 and status == "failed" and code not in UNKNOWN_CODES:
                    raise RuntimeError(f"Swap failed: {res.get('error') or status} (code {code})")
                last = f"{res.get('error') or status or r.status_code} (code {code})"
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Swap refused: HTTP {e.response.status_code}") from None
            except (httpx.TransportError, ValueError) as e:  # timeout, dropped connection, bad JSON: outcome unknown
                last = type(e).__name__
            if time.monotonic() > deadline:
                raise SwapOutcomeUnknown(f"swap outcome unknown after {self.poll_seconds:.0f}s (last: {last})")
            log.warning("Swap not confirmed yet (%s); polling again", last)
            self.sleep(5)


def is_valid_address(s: str) -> bool:
    try:
        Pubkey.from_string(s)
        return True
    except Exception:
        return False
