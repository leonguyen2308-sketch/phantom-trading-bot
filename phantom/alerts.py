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


class XPoster:
    """Posts trades (BOUGHT / SOLD) to X. Everything else (kill switch, errors) stays private.

    Uses the X API v2 with OAuth 1.0a user keys from developer.x.com (app needs Read and Write).
    """

    URL = "https://api.x.com/2/tweets"
    POST_PREFIXES = ("BOUGHT", "SOLD")

    def __init__(self, api_key: str, api_secret: str, access_token: str, access_secret: str,
                 http: httpx.Client | None = None, hashtags: str = "#BTC #Bitcoin"):
        self._keys = (api_key, api_secret, access_token, access_secret)  # never log these
        self.http = http or httpx.Client(timeout=20)
        self.hashtags = hashtags.strip()

    def should_post(self, text: str) -> bool:
        body = text.split("] ", 1)[-1]  # drop the "[PAPER] " / "[LIVE] " tag
        return body.startswith(self.POST_PREFIXES)

    def send(self, text: str) -> None:
        if not self.should_post(text):
            return
        tweet = f"{text}\n{self.hashtags}".strip() if self.hashtags else text
        r = self.http.post(self.URL, json={"text": tweet[:280]}, headers={"Authorization": self._auth("POST", self.URL)})
        r.raise_for_status()

    def _auth(self, method: str, url: str, nonce: str | None = None, ts: str | None = None) -> str:
        import base64, hashlib, hmac, secrets as _s
        from urllib.parse import quote
        ck, cs, tk, ts_ = self._keys
        q = lambda s: quote(str(s), safe="")
        params = {"oauth_consumer_key": ck, "oauth_nonce": nonce or _s.token_hex(16),
                  "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": ts or str(int(time.time())),
                  "oauth_token": tk, "oauth_version": "1.0"}
        # JSON body is not part of the signature, only oauth_* params.
        param_str = "&".join(f"{q(k)}={q(v)}" for k, v in sorted(params.items()))
        base = "&".join([method.upper(), q(url), q(param_str)])
        key = f"{q(cs)}&{q(ts_)}".encode()
        params["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{q(k)}="{q(v)}"' for k, v in sorted(params.items()))


class Fanout:
    """Send each alert to several channels; one failing never stops the others."""

    def __init__(self, *channels):
        self.channels = [c for c in channels if c]

    def send(self, text: str) -> None:
        for c in self.channels:
            try:
                c.send(text)
            except Exception as e:
                log.warning("%s send failed: %s", type(c).__name__, type(e).__name__)

    def listen(self, handlers):
        for c in self.channels:
            if hasattr(c, "listen"):
                return c.listen(handlers)
