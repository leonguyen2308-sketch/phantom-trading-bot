"""Telegram alerts and commands. Only your own chat id can command the bot."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import httpx

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        self._url = f"https://api.telegram.org/bot{token}"  # contains the token: never log it
        self.chat_id = str(chat_id)
        self.http = http or httpx.Client(timeout=35)

    def send(self, text: str) -> None:
        self.http.post(f"{self._url}/sendMessage", json={"chat_id": self.chat_id, "text": text[:4000]}).raise_for_status()

    def listen(self, handlers: dict[str, Callable[[], str]]) -> threading.Thread:
        """Poll for commands like /kill and /status in a background thread."""
        def loop() -> None:
            offset = 0
            while True:
                try:
                    r = self.http.get(f"{self._url}/getUpdates", params={"timeout": 30, "offset": offset})
                    for u in r.json().get("result", []):
                        offset = u["update_id"] + 1
                        msg = u.get("message") or {}
                        if str(msg.get("chat", {}).get("id")) != self.chat_id:
                            continue  # ignore everyone else
                        cmd = (msg.get("text") or "").strip().split()[0].split("@")[0].lower() if msg.get("text") else ""
                        if cmd in handlers:
                            self.send(handlers[cmd]())
                except Exception as e:
                    log.warning("Telegram polling error: %s", type(e).__name__)
                    time.sleep(5)
        t = threading.Thread(target=loop, daemon=True, name="telegram")
        t.start()
        return t
