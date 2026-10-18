"""Solana RPC: read wallets, transactions and token mints. The RPC URL carries your API key: never log it."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class RpcError(RuntimeError):
    pass


@dataclass
class MintInfo:
    mint: str
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    program: str  # "spl-token" or "spl-token-2022"
    extensions: list[dict] = field(default_factory=list)


class SolanaRpc:
    def __init__(self, url: str, http: httpx.Client | None = None, min_interval: float = 0.11):
        self.url = url
        self.http = http or httpx.Client(timeout=20)
        self.min_interval = min_interval  # about 9 requests a second: under the free Helius limit
        self._last = 0.0

    def call(self, method: str, params: list):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise RpcError(f"Solana RPC {method} failed: {type(e).__name__}") from None
        if "error" in body:
            raise RpcError(f"Solana RPC {method} failed: {body['error'].get('message', body['error'])}")
        return body["result"]

    def signatures(self, address: str, limit: int = 50, before: str | None = None, until: str | None = None) -> list[dict]:
        """Newest first: [{signature, slot, err, blockTime}]."""
        opts: dict = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return self.call("getSignaturesForAddress", [address, opts])

    def transaction(self, sig: str) -> dict | None:
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                  "commitment": "confirmed"}])

    def sol_balance(self, address: str) -> float:
        return self.call("getBalance", [address, {"commitment": "confirmed"}])["value"] / 1e9

    def token_balances(self, owner: str) -> dict[str, int]:
        """Every token the wallet holds, in base units, across both token programs."""
        out: dict[str, int] = {}
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            res = self.call("getTokenAccountsByOwner", [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for a in res["value"]:
                info = a["account"]["data"]["parsed"]["info"]
                out[info["mint"]] = out.get(info["mint"], 0) + int(info["tokenAmount"]["amount"])
        return out

    def mint_info(self, mint: str) -> MintInfo:
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        v = res.get("value") if res else None
        if not v or not isinstance(v.get("data"), dict) or v["data"].get("parsed", {}).get("type") != "mint":
            raise RpcError(f"{mint} is not a token mint")
        info = v["data"]["parsed"]["info"]
        return MintInfo(mint=mint, decimals=int(info["decimals"]), mint_authority=info.get("mintAuthority"),
                        freeze_authority=info.get("freezeAuthority"), program=v["data"].get("program", ""),
                        extensions=list(info.get("extensions") or []))


def ws_url(rpc_url: str) -> str:
    """Helius, QuickNode and most providers serve websockets on the same URL with wss://."""
    if rpc_url.startswith("https://"):
        return "wss://" + rpc_url[len("https://"):]
    if rpc_url.startswith("http://"):
        return "ws://" + rpc_url[len("http://"):]
    return rpc_url
