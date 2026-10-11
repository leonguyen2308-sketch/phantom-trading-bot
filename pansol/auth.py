"""Sign in with Phantom. The wallet signs a one-time text message: free, not a transaction, moves nothing.

The server keeps nothing for a sign-in until a valid signature arrives: the challenge it hands out is
HMAC-signed, so it can be checked later without a database row. Only used nonces are stored, to block
replays, which means a flood of unsigned requests costs the server nothing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
from datetime import datetime, timezone

from solders.pubkey import Pubkey
from solders.signature import Signature

NONCE_TTL = 300                 # seconds to sign before the message expires
USER_SESSION_TTL = 7 * 86400
ADMIN_SESSION_TTL = 12 * 3600
COOKIE = "pansol_session"

STATEMENT = (
    "Sign in to Pansol Meme Tracker. This is free: it is a message, not a transaction, "
    "and it cannot move your coins."
)


def parse_address(value: object) -> str | None:
    """The canonical base58 form of a Solana address, or None if it is not one."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return None
    try:
        key = Pubkey.from_string(value)
    except ValueError:
        return None
    return value if str(key) == value else None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_in_message(domain: str, uri: str, wallet: str, nonce: str, issued_at: int) -> str:
    """Sign In With Solana layout, so Phantom shows it as a sign-in and checks the site's domain."""
    return (
        f"{domain} wants you to sign in with your Solana account:\n"
        f"{wallet}\n"
        f"\n"
        f"{STATEMENT}\n"
        f"\n"
        f"URI: {uri}\n"
        f"Version: 1\n"
        f"Chain ID: mainnet\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_iso(issued_at)}\n"
        f"Expiration Time: {_iso(issued_at + NONCE_TTL)}"
    )


# ---------- challenges ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mac(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def make_challenge(secret: bytes, wallet: str, domain: str, uri: str, issued_at: int) -> tuple[str, str]:
    """Return (challenge token for the browser to send back, message for the wallet to sign)."""
    nonce = secrets.token_hex(12)
    payload = _b64url(json.dumps({"w": wallet, "d": domain, "u": uri, "t": issued_at, "n": nonce},
                                 separators=(",", ":")).encode())
    return f"{payload}.{_mac(secret, payload)}", sign_in_message(domain, uri, wallet, nonce, issued_at)


def open_challenge(secret: bytes, token: object, now: int) -> dict | None:
    """The challenge's fields and rebuilt message, or None if it was forged, mangled or has expired."""
    if not isinstance(token, str) or len(token) > 1000 or token.count(".") != 1:
        return None
    payload, mac = token.split(".")
    if not hmac.compare_digest(mac, _mac(secret, payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        wallet, domain, uri, issued_at, nonce = data["w"], data["d"], data["u"], int(data["t"]), data["n"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if not issued_at <= now < issued_at + NONCE_TTL:
        return None
    return {
        "wallet": wallet,
        "nonce": nonce,
        "expires_at": issued_at + NONCE_TTL,
        "message": sign_in_message(domain, uri, wallet, nonce, issued_at),
    }


# ---------- signatures ----------

_OFFCHAIN = b"\xffsolana offchain"


def _signed_forms(message: bytes, signer: bytes) -> list[bytes]:
    """The bytes a wallet may actually sign for this message.

    Software wallets sign the raw text. Hardware wallets (a Ledger inside Phantom) wrap it in Solana's
    off-chain message envelope; both published layouts are accepted. Every form still contains the
    whole message, so the nonce and domain stay bound to the signature.
    """
    forms = [message]
    if len(message) <= 0xFFFF:
        length = struct.pack("<H", len(message))
        for fmt in (0, 1, 2):
            forms.append(_OFFCHAIN + bytes([0, fmt]) + length + message)
            forms.append(_OFFCHAIN + bytes([0]) + bytes(32) + bytes([fmt, 1]) + signer + length + message)
    return forms


def verify(wallet: str, message: str, signature_b64: object) -> bool:
    if not isinstance(signature_b64, str) or len(signature_b64) > 200:
        return False
    try:
        raw = base64.b64decode(signature_b64, validate=True)
        if len(raw) != 64:
            return False
        sig = Signature.from_bytes(raw)
        key = Pubkey.from_string(wallet)
    except (binascii.Error, ValueError):
        return False
    return any(sig.verify(key, form) for form in _signed_forms(message.encode("utf-8"), bytes(key)))


# ---------- sessions ----------

def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_secret() -> bytes:
    return secrets.token_bytes(32)
