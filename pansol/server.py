"""Pansol web server: the launch page, the meme tracker and the admin view.

Users sign in by signing a message in Phantom and see only their own wallet. Wallets listed in
PANSOL_ADMIN_WALLETS also get /api/admin/*, the list of every tracker user. That check runs on the
server for every admin request, so a normal wallet never receives other people's data.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pansol import auth
from pansol.chain import Chain, ChainError
from pansol.store import Busy, Store

log = logging.getLogger("pansol.server")

WEB = Path(__file__).resolve().parent
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")

SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@dataclass(frozen=True)
class Settings:
    admin_wallets: frozenset[str] = frozenset()
    rpc_url: str = DEFAULT_RPC
    db_path: str = "state/pansol.db"
    public_url: str = ""            # e.g. https://pansol.xyz; empty = use the address the visitor typed
    pansol_mint: str | None = None  # set after launch to pin Pansol on every tracker
    watch_limit: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        admins = set()
        for part in re.split(r"[\s,]+", env.get("PANSOL_ADMIN_WALLETS", "").strip()):
            if not part:
                continue
            address = auth.parse_address(part)
            if not address:
                raise ValueError(f"PANSOL_ADMIN_WALLETS: {part!r} is not a Solana wallet address")
            admins.add(address)

        mint = env.get("PANSOL_MINT", "").strip() or None
        if mint and not auth.parse_address(mint):
            raise ValueError(f"PANSOL_MINT: {mint!r} is not a Solana token address")

        public_url = env.get("PANSOL_PUBLIC_URL", "").strip().rstrip("/")
        if public_url and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$", public_url):
            raise ValueError("PANSOL_PUBLIC_URL must look like https://pansol.xyz (no path)")

        return cls(
            admin_wallets=frozenset(admins),
            rpc_url=env.get("SOLANA_RPC_URL", "").strip() or DEFAULT_RPC,
            db_path=env.get("PANSOL_DB", "").strip() or "state/pansol.db",
            public_url=public_url,
            pansol_mint=mint,
        )


class WalletIn(BaseModel):
    wallet: str = Field(max_length=64)


class VerifyIn(BaseModel):
    nonce: str = Field(max_length=64)
    signature: str = Field(max_length=200)


class MintIn(BaseModel):
    mint: str = Field(max_length=100)


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _csv_cell(value) -> str:
    """Stop spreadsheet apps from running coin names that start with = + - @ as formulas."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def make_app(
    settings: Settings,
    store: Store | None = None,
    chain: Chain | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    store = store or Store(settings.db_path)
    chain = chain or Chain(settings.rpc_url)
    app = FastAPI(title="Pansol", docs_url=None, redoc_url=None, openapi_url=None)
    public = urlsplit(settings.public_url) if settings.public_url else None

    def now() -> int:
        return int(clock())

    def is_https(request: Request) -> bool:
        if public:
            return public.scheme == "https"
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or forwarded == "https"

    def site(request: Request) -> tuple[str, str]:
        """The domain and URL that go into the sign-in message. Phantom warns if they differ from the page."""
        if public:
            return public.netloc, settings.public_url
        host = request.headers.get("host", "")
        if not HOST_RE.match(host):
            raise HTTPException(400, "Bad Host header.")
        return host, f"{'https' if is_https(request) else 'http'}://{host}"

    def current_wallet(request: Request) -> str | None:
        token = request.cookies.get(auth.COOKIE)
        if not token or len(token) > 100:
            return None
        return store.session_wallet(auth.token_hash(token), now())

    def require_user(request: Request) -> str:
        wallet = current_wallet(request)
        if not wallet:
            raise HTTPException(401, "Sign in with Phantom first.")
        return wallet

    def require_admin(request: Request) -> str:
        wallet = require_user(request)
        if wallet not in settings.admin_wallets:
            raise HTTPException(403, "This wallet is not an admin.")
        return wallet

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Refuse writes sent from other websites (a second lock on top of SameSite cookies).
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            allowed = {request.headers.get("host", "")} | ({public.netloc} if public else set())
            if origin and urlsplit(origin).netloc not in allowed:
                return JSONResponse({"detail": "Requests from other websites are refused."}, status_code=403)
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---------- pages ----------

    @app.get("/", include_in_schema=False)
    def launch_page():
        return FileResponse(WEB / "index.html")

    @app.get("/tracker", include_in_schema=False)
    def tracker_page():
        return FileResponse(WEB / "tracker.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(WEB / "admin.html", headers={"X-Robots-Tag": "noindex, nofollow"})

    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    # ---------- sign in with Phantom ----------

    @app.post("/api/auth/nonce")
    def sign_in_start(body: WalletIn, request: Request):
        wallet = auth.parse_address(body.wallet)
        if not wallet:
            raise HTTPException(400, "That is not a Solana wallet address.")
        domain, uri = site(request)
        nonce, t = auth.new_nonce(), now()
        message = auth.sign_in_message(domain, uri, wallet, nonce, t)
        try:
            store.add_nonce(nonce, wallet, message, t + auth.NONCE_TTL, t)
        except Busy:
            raise HTTPException(503, "Too many sign-ins right now. Try again in a minute.")
        return {"nonce": nonce, "message": message}

    @app.post("/api/auth/verify")
    def sign_in_finish(body: VerifyIn, request: Request, response: Response):
        pending = store.take_nonce(body.nonce, now())
        if not pending:
            raise HTTPException(400, "This sign-in expired. Press Connect again.")
        wallet = pending["wallet"]
        if not auth.verify(wallet, pending["message"], body.signature):
            raise HTTPException(401, "The signature does not match this wallet.")

        old = request.cookies.get(auth.COOKIE)
        if old:
            store.drop_session(auth.token_hash(old))
        is_admin = wallet in settings.admin_wallets
        ttl = auth.ADMIN_SESSION_TTL if is_admin else auth.USER_SESSION_TTL
        token = auth.new_session_token()
        store.record_sign_in(wallet, now())
        store.add_session(auth.token_hash(token), wallet, now() + ttl, now())
        response.set_cookie(
            auth.COOKIE, token, max_age=ttl, path="/", httponly=True, samesite="lax", secure=is_https(request)
        )
        log.info("sign-in %s%s", wallet, " (admin)" if is_admin else "")
        return {"wallet": wallet, "isAdmin": is_admin}

    @app.post("/api/auth/logout")
    def sign_out(request: Request, response: Response):
        token = request.cookies.get(auth.COOKIE)
        if token:
            store.drop_session(auth.token_hash(token))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        wallet = current_wallet(request)
        return {"wallet": wallet, "isAdmin": bool(wallet) and wallet in settings.admin_wallets}

    # ---------- tracker (each user sees only their own wallet) ----------

    @app.get("/api/portfolio")
    def portfolio(request: Request):
        wallet = require_user(request)
        try:
            result = chain.portfolio(wallet, store.watchlist(wallet), settings.pansol_mint)
        except ChainError as e:
            log.warning("portfolio %s: %s", wallet, e)
            raise HTTPException(502, "Couldn't reach the Solana network. Try again in a minute.")
        t = now()
        store.save_snapshot(wallet, result["total_usd"], result["holdings"], t)
        store.touch(wallet, t)
        return result

    @app.post("/api/watchlist")
    def watch_add(body: MintIn, request: Request):
        wallet = require_user(request)
        mint = auth.parse_address(body.mint.strip())
        if not mint:
            raise HTTPException(400, "Paste the coin's token address (its contract address, CA).")
        if not store.add_watch(wallet, mint, now(), settings.watch_limit):
            raise HTTPException(409, f"Your watchlist is full ({settings.watch_limit} coins). Remove one first.")
        return {"watchlist": store.watchlist(wallet)}

    @app.delete("/api/watchlist/{mint}")
    def watch_remove(mint: str, request: Request):
        wallet = require_user(request)
        store.remove_watch(wallet, mint)
        return {"watchlist": store.watchlist(wallet)}

    # ---------- admin only ----------

    @app.get("/api/admin/users")
    def admin_users(request: Request):
        require_admin(request)
        users = store.users()
        for u in users:
            u["is_admin"] = u["wallet"] in settings.admin_wallets
        return {"stats": store.stats(now()), "users": users, "now": now()}

    @app.get("/api/admin/users.csv")
    def admin_users_csv(request: Request):
        require_admin(request)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["wallet", "first_seen_utc", "last_seen_utc", "sign_ins", "portfolio_usd",
                    "coins", "watchlist", "top_coins"])
        for u in store.users():
            top = "; ".join(
                f"{h.get('symbol') or h.get('mint', '')[:6]} ${h['value_usd']:.2f}"
                for h in u["holdings"][:5] if h.get("value_usd") is not None
            )
            w.writerow([_csv_cell(v) for v in (
                u["wallet"], _iso(u["first_seen"]), _iso(u["last_seen"]), u["sign_ins"],
                "" if u["value_usd"] is None else f"{u['value_usd']:.2f}",
                u["token_count"] if u["token_count"] is not None else "", u["watch_count"], top,
            )])
        return Response(
            out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="pansol-tracker-users.csv"'},
        )

    @app.get("/api/admin/users/{wallet}")
    def admin_user(wallet: str, request: Request, live: bool = False):
        require_admin(request)
        address = auth.parse_address(wallet)
        user = store.user(address) if address else None
        if not user:
            raise HTTPException(404, "No tracker user with this wallet.")
        watch = store.watchlist(address)
        live_portfolio = None
        if live:
            try:
                live_portfolio = chain.portfolio(address, watch)
            except ChainError as e:
                log.warning("admin live portfolio %s: %s", address, e)
                raise HTTPException(502, "Couldn't reach the Solana network. Try again in a minute.")
            store.save_snapshot(address, live_portfolio["total_usd"], live_portfolio["holdings"], now())
            user = store.user(address)
        user["is_admin"] = address in settings.admin_wallets
        return {"user": user, "watchlist": watch, "portfolio": live_portfolio}

    return app
