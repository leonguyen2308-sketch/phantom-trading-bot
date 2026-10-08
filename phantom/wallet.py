"""The bot wallet's key. Use a NEW Phantom wallet made only for the bot, never your main one."""

from __future__ import annotations

from solders.keypair import Keypair


def load_keypair(secret_base58: str, expected_address: str) -> Keypair:
    """Load the private key Phantom exports (base58) and check it matches the address you expect.

    Errors never include the secret.
    """
    try:
        kp = Keypair.from_base58_string(secret_base58.strip())
    except Exception:
        raise ValueError("PHANTOM_BOT_PRIVATE_KEY is not a valid Solana private key") from None
    if str(kp.pubkey()) != expected_address.strip():
        raise ValueError(f"The private key belongs to {kp.pubkey()}, not PHANTOM_BOT_ADDRESS {expected_address.strip()}")
    return kp
