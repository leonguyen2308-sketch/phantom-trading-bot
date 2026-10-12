"""Keep a free Render instance awake by visiting its own public address.

Render's free plan stops a service after 15 minutes without incoming requests, and the next
visitor then waits about a minute. A request to the public address every 10 minutes counts as
incoming traffic. One always-on service fits in the free plan's 750 hours a month.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import httpx

log = logging.getLogger("pansol.keepalive")

INTERVAL = 600  # seconds; Render sleeps after 15 idle minutes


def keepalive_url(env) -> str | None:
    """The address to ping, or None when not on Render or turned off with PANSOL_KEEP_AWAKE=0."""
    if env.get("PANSOL_KEEP_AWAKE", "").strip().lower() in ("0", "no", "false", "off"):
        return None
    base = env.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    return f"{base}/healthz" if base.startswith("https://") else None


def run(url: str, stop: threading.Event, get: Callable[[str], object] | None = None, interval: float = INTERVAL) -> None:
    get = get or (lambda u: httpx.get(u, timeout=15))
    while not stop.wait(interval):
        try:
            get(url)
        except Exception as e:   # a missed ping only means the site may nap once
            log.warning("keep-awake ping failed: %s", e)


def start(url: str) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run, args=(url, stop), daemon=True, name="keepalive").start()
    log.info("Keeping the site awake: visiting %s every %d minutes", url, INTERVAL // 60)
    return stop
