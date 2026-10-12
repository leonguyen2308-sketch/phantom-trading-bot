"""Run the Pansol site and meme tracker:  uv run python -m pansol  (with PostgreSQL: uv run --extra postgres ...)"""

from __future__ import annotations

import logging
import os
import sys
from urllib.parse import unquote, urlsplit

import uvicorn

from pansol import keepalive
from pansol.server import Settings, make_app
from pansol.store import is_postgres
from phantom.config import install_redaction, load_env


def main() -> None:
    load_env(".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except ValueError as e:
        sys.exit(f"Config error: {e}")
    # Keep the RPC URL (API key) and the database URL and password out of the logs.
    raw = [os.environ.get("PANSOL_DB", ""), os.environ.get("DATABASE_URL", ""), settings.db_path]
    secrets = [v for v in raw if is_postgres(v.strip().strip("'\""))]   # a SQLite path is not a secret
    if is_postgres(settings.db_path):
        password = urlsplit(settings.db_path).password or ""
        secrets += [password, unquote(password)]
    install_redaction(secrets)
    host = os.environ.get("HOST", "127.0.0.1")
    if not settings.admin_wallets:
        logging.warning("PANSOL_ADMIN_WALLETS is empty: nobody can open the admin view.")
    if settings.public_urls:
        logging.info("Sign-in works on: %s", ", ".join(settings.public_urls))
    elif host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("PANSOL_PUBLIC_URL is empty: sign-in only works on localhost until you set it.")

    port = int(os.environ.get("PORT", "8080"))
    if is_postgres(settings.db_path):
        logging.info("Database: PostgreSQL at %s", urlsplit(settings.db_path).hostname)
    else:
        logging.info("Database: SQLite file %s", settings.db_path)
    try:
        app = make_app(settings)
    except Exception as e:   # most often a wrong database URL or an unreachable database
        message = f"Could not open the database: {type(e).__name__}: {e}"
        for secret in secrets:
            if secret and len(secret) >= 4:
                message = message.replace(secret, "[REDACTED]")
        sys.exit(message)
    ping = keepalive.keepalive_url(os.environ)
    if ping:
        keepalive.start(ping)
    print(f"Pansol: http://{host}:{port}   tracker: /tracker   admin: /admin")
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
