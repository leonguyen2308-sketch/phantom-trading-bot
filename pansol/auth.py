"""Sign in with Phantom. The wallet signs a one-time text message: free, not a transaction, moves nothing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
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


def new_nonce() -> str:
    return secrets.token_hex(16)


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


def verify(wallet: str, message: str, signature_b64: object) -> bool:
    if not isinstance(signature_b64, str) or len(signature_b64) > 200:
        return False
    try:
        raw = base64.b64decode(signature_b64, validate=True)
        if len(raw) != 64:
            return False
        return Signature.from_bytes(raw).verify(Pubkey.from_string(wallet), message.encode("utf-8"))
    except (binascii.Error, ValueError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
