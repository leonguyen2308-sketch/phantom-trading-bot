import base64
import csv
import io
import json
import os
import re
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair

from pansol import auth
from pansol.chain import DEXSCREENER_TOKENS, SOL_MINT, TOKEN_PROGRAMS, Chain
from pansol.server import Settings, make_app
from pansol.store import Store

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
NFT = "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj"
SPAM = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


# Set PANSOL_TEST_DATABASE_URL=postgresql://.../<name ending in "test"> to run every test against PostgreSQL.
# It DROPS all Pansol tables in that database, so never point it at the real one.
TEST_PG = os.environ.get("PANSOL_TEST_DATABASE_URL")
_open_stores = []


def fresh_store():
    """An empty store. On PostgreSQL the tables are dropped first and older stores are closed."""
    if not TEST_PG:
        return Store(":memory:")
    if not urlsplit(TEST_PG).path.rstrip("/").endswith("test"):
        pytest.exit("PANSOL_TEST_DATABASE_URL must name a database ending in 'test' (the tests drop its tables)")
    import psycopg
    while _open_stores:
        _open_stores.pop().close()
    with psycopg.connect(TEST_PG, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS users, watchlist, used_nonces, sessions, meta")
    store = Store(TEST_PG)
    _open_stores.append(store)
    return store


class Clock:
    def __init__(self, t=1_791_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def token_account(mint, ui, decimals=6):
    return {"pubkey": "x", "account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"amount": str(int(ui * 10 ** decimals)), "decimals": decimals,
                                      "uiAmount": ui, "uiAmountString": str(ui)}}}}}}


def pair(mint, symbol, price, liquidity, change=5.0, quote=False):
    base = {"address": mint, "name": symbol.title(), "symbol": symbol}
    other = {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "name": "USD Coin", "symbol": "USDC"}
    return {
        "url": f"https://dexscreener.com/solana/{symbol.lower()}pair",
        "baseToken": other if quote else base,
        "quoteToken": base if quote else other,
        "priceUsd": str(price),
        "priceChange": {"h24": change},
        "liquidity": {"usd": liquidity},
        "marketCap": price * 1e9,
        "volume": {"h24": 1000},
        "info": {"imageUrl": f"https://cdn.example/{symbol}.png"},
    }


class FakeNet:
    """Answers the Solana RPC and DexScreener like the real services."""

    def __init__(self):
        self.wallets = {}          # wallet -> (lamports, {program: [accounts]})
        self.dex_down = False
        self.rpc_down = False
        self.dex_calls = 0
        self.symbols = {}          # mint -> symbol override
        self.requested = []        # mints asked of DexScreener, per call

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "rpc.test":
            if self.rpc_down:
                return httpx.Response(503, text="down")
            body = json.loads(request.content)
            wallet = body["params"][0]
            lamports, accounts = self.wallets.get(wallet, (0, {}))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": lamports}})
            program = body["params"][1]["programId"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"value": accounts.get(program, [])}})
        if str(request.url).startswith(DEXSCREENER_TOKENS):
            self.dex_calls += 1
            if self.dex_down:
                return httpx.Response(429, text="slow down")
            mints = str(request.url)[len(DEXSCREENER_TOKENS):].split(",")
            self.requested.append(mints)
            pairs = []
            if SOL_MINT in mints:
                pairs.append(pair(SOL_MINT, "SOL", 150.0, 9e6))
            if BONK in mints:
                pairs += [pair(BONK, "BONK", 0.00002, 5e5), pair(BONK, "BONK", 0.000021, 9e6, change=-3.5),
                          pair(BONK, "BONK", 999.0, 1e9, quote=True)]   # BONK as the quote token: ignored
            if WIF in mints:
                p = pair(WIF, self.symbols.get(WIF, "<img src=x onerror=alert(1)>"), 2.0, 1e6)
                p["info"]["imageUrl"] = "javascript:alert(1)"
                pairs.append(p)
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)


@pytest.fixture
def env():
    admin, user, other = Keypair(), Keypair(), Keypair()
    net = FakeNet()
    clock = Clock()
    chain = Chain("https://rpc.test/", http=httpx.Client(transport=httpx.MockTransport(net.handler)), clock=clock)
    settings = Settings(admin_wallets=frozenset({str(admin.pubkey())}), rpc_url="https://rpc.test/",
                        db_path=":memory:", public_urls=("http://testserver",))
    app = make_app(settings, store=fresh_store(), chain=chain, clock=clock)
    return {"app": app, "net": net, "clock": clock, "admin": admin, "user": user, "other": other}


def client(env):
    return TestClient(env["app"])


def sign_in(c, kp):
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())})
    assert start.status_code == 200, start.text
    msg = start.json()["message"]
    sig = base64.b64encode(bytes(kp.sign_message(msg.encode()))).decode()
    return c.post("/api/auth/verify", json={"challenge": start.json()["challenge"], "signature": sig})


# ---------- sign in ----------

def test_sign_in_message_is_siws_and_free(env):
    c = client(env)
    kp = env["user"]
    msg = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()["message"]
    assert msg.startswith(f"testserver wants you to sign in with your Solana account:\n{kp.pubkey()}\n")
    assert "not a transaction" in msg and "Nonce: " in msg and "URI: http://testserver" in msg


def test_sign_in_sets_session_and_me(env):
    c = client(env)
    r = sign_in(c, env["user"])
    assert r.status_code == 200
    assert r.json() == {"wallet": str(env["user"].pubkey()), "isAdmin": False}
    assert auth.COOKIE in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/me").json()["wallet"] == str(env["user"].pubkey())


def test_signature_from_another_key_is_refused(env):
    c = client(env)
    start = c.post("/api/auth/nonce", json={"wallet": str(env["admin"].pubkey())}).json()
    forged = base64.b64encode(bytes(env["user"].sign_message(start["message"].encode()))).decode()
    r = c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": forged})
    assert r.status_code == 401
    assert c.get("/api/me").json()["wallet"] is None


def test_challenge_is_single_use_and_expires(env):
    c = client(env)
    kp = env["user"]
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    sig = base64.b64encode(bytes(kp.sign_message(start["message"].encode()))).decode()
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code == 200
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code == 400

    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    sig = base64.b64encode(bytes(kp.sign_message(start["message"].encode()))).decode()
    env["clock"].t += auth.NONCE_TTL + 1
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code == 400


@pytest.mark.parametrize("bad", ["", "hello", "0" * 44, "O" * 44, 123])
def test_bad_wallet_address_is_refused(env, bad):
    assert client(env).post("/api/auth/nonce", json={"wallet": bad}).status_code in (400, 422)


@pytest.mark.parametrize("sig", ["", "not base64!!", base64.b64encode(b"x" * 63).decode(), "A" * 300])
def test_malformed_signature_is_refused(env, sig):
    c = client(env)
    start = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())}).json()
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code in (401, 422)


def test_session_expires_and_logout_ends_it(env):
    c = client(env)
    sign_in(c, env["user"])
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/me").json()["wallet"] is None

    sign_in(c, env["user"])
    env["clock"].t += auth.USER_SESSION_TTL + 1
    assert c.get("/api/me").json()["wallet"] is None


def test_cross_site_writes_are_refused(env):
    c = client(env)
    r = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())}, headers={"origin": "http://testserver"})
    assert r.status_code == 200


# ---------- tracker ----------

def give(env, kp, lamports, tokens=None, t22=None):
    env["net"].wallets[str(kp.pubkey())] = (lamports, {TOKEN_PROGRAMS[0]: tokens or [], TOKEN_PROGRAMS[1]: t22 or []})


def test_portfolio_values_coins_and_skips_nfts(env):
    c = client(env)
    give(env, env["user"], 2_000_000_000,
         tokens=[token_account(BONK, 1_000_000), token_account(NFT, 1, decimals=0), token_account(SPAM, 0)],
         t22=[token_account(WIF, 10)])
    sign_in(c, env["user"])
    p = c.get("/api/portfolio").json()

    by_mint = {h["mint"]: h for h in p["holdings"]}
    assert set(by_mint) == {SOL_MINT, BONK, WIF}                       # NFT and empty account left out
    assert by_mint[SOL_MINT]["value_usd"] == pytest.approx(300.0)
    assert by_mint[BONK]["price_usd"] == pytest.approx(0.000021)      # deepest pool wins, quote-side pair ignored
    assert by_mint[BONK]["change_24h"] == -3.5
    assert by_mint[WIF]["image"] is None                              # javascript: URL dropped
    assert p["total_usd"] == pytest.approx(300 + 21 + 20)
    assert [h["mint"] for h in p["holdings"]] == [SOL_MINT, BONK, WIF]   # biggest value first


def test_portfolio_survives_price_outage(env):
    c = client(env)
    give(env, env["user"], 1_000_000_000, tokens=[token_account(BONK, 5)])
    env["net"].dex_down = True
    sign_in(c, env["user"])
    p = c.get("/api/portfolio").json()
    assert {h["mint"] for h in p["holdings"]} == {SOL_MINT, BONK}
    assert all(h["price_usd"] is None for h in p["holdings"])
    assert p["total_usd"] == 0 and p["prices_ok"] is False


def test_price_outage_keeps_last_prices_and_backs_off(env):
    c = client(env)
    spam = [str(Keypair().pubkey()) for _ in range(70)]
    give(env, env["user"], 1_000_000_000, tokens=[token_account(BONK, 1_000_000)] + [token_account(m, 1) for m in spam])
    sign_in(c, env["user"])
    first = c.get("/api/portfolio").json()
    assert first["total_usd"] == pytest.approx(171.0) and first["prices_ok"]

    env["net"].dex_down = True
    env["clock"].t += 31                                   # prices are stale, DexScreener now fails
    calls = env["net"].dex_calls
    p = c.get("/api/portfolio").json()
    assert env["net"].dex_calls == calls + 1               # one failed call, not one per 30-coin chunk
    assert p["total_usd"] == pytest.approx(171.0)          # last known prices still shown
    c.get("/api/portfolio")
    assert env["net"].dex_calls == calls + 1               # cooling down: no more calls

    env["net"].dex_down = False
    env["clock"].t += 31
    c.get("/api/portfolio")
    assert env["net"].dex_calls > calls + 1                # back to normal


def test_price_outage_never_overwrites_the_snapshot(env):
    c = client(env)
    give(env, env["user"], 1_000_000_000, tokens=[token_account(BONK, 1_000_000)])
    sign_in(c, env["user"])
    c.get("/api/portfolio")
    give(env, env["user"], 1_000_000_000, tokens=[token_account(BONK, 1_000_000), token_account(WIF, 5)])
    env["net"].dex_down = True
    env["clock"].t += 31
    p = c.get("/api/portfolio").json()
    assert p["prices_ok"] is False                          # WIF is new and has no price at all

    a = client(env)
    sign_in(a, env["admin"])
    row = next(u for u in a.get("/api/admin/users").json()["users"] if u["wallet"] == str(env["user"].pubkey()))
    assert row["value_usd"] == pytest.approx(171.0) and row["token_count"] == 2


def test_big_wallets_list_the_valuable_coins_first(env):
    c = client(env)
    spam = [str(Keypair().pubkey()) for _ in range(305)]
    give(env, env["user"], 0, tokens=[token_account(m, 1e9) for m in spam], t22=[token_account(WIF, 500)])
    sign_in(c, env["user"])
    p = c.get("/api/portfolio").json()
    assert p["holdings"][0]["mint"] == WIF                  # a small amount of a real coin beats spam
    assert p["total_usd"] == pytest.approx(1000.0)
    assert len(p["holdings"]) == 300 and p["hidden_tokens"] == 6 and p["coin_count"] == 306


def test_coins_without_a_market_are_rechecked_slowly(env):
    c = client(env)
    spam = str(Keypair().pubkey())
    give(env, env["user"], 0, tokens=[token_account(spam, 5)])
    sign_in(c, env["user"])
    c.get("/api/portfolio")
    asked = lambda: [m for call in env["net"].requested for m in call].count(spam)  # noqa: E731
    assert asked() == 1
    env["clock"].t += 60
    c.get("/api/portfolio")
    assert asked() == 1                                     # still known to have no market
    c.post("/api/watchlist", json={"mint": spam})
    env["clock"].t += 31
    c.get("/api/portfolio")
    assert asked() == 2                                     # watched coins are checked at the normal pace
    env["clock"].t += 600
    c.delete(f"/api/watchlist/{spam}")
    c.get("/api/portfolio")
    assert asked() == 3                                     # and held ones again after the long wait


def test_portfolio_reports_rpc_outage(env):
    c = client(env)
    env["net"].rpc_down = True
    sign_in(c, env["user"])
    r = c.get("/api/portfolio")
    assert r.status_code == 502 and "Solana" in r.json()["detail"]


def test_prices_are_cached(env):
    c = client(env)
    give(env, env["user"], 1_000_000_000, tokens=[token_account(BONK, 5)])
    sign_in(c, env["user"])
    c.get("/api/portfolio")
    calls = env["net"].dex_calls
    c.get("/api/portfolio")
    assert env["net"].dex_calls == calls
    env["clock"].t += 31
    c.get("/api/portfolio")
    assert env["net"].dex_calls == calls + 1


def test_tracker_needs_sign_in(env):
    c = client(env)
    assert c.get("/api/portfolio").status_code == 401
    assert c.post("/api/watchlist", json={"mint": BONK}).status_code == 401


def test_watchlist_add_remove_limit_and_privacy(env):
    c = client(env)
    sign_in(c, env["user"])
    assert c.post("/api/watchlist", json={"mint": BONK}).json()["watchlist"] == [BONK]
    assert c.post("/api/watchlist", json={"mint": BONK}).json()["watchlist"] == [BONK]   # no duplicates
    assert c.post("/api/watchlist", json={"mint": "not a mint"}).status_code == 400
    p = c.get("/api/portfolio").json()
    assert p["watchlist"][0]["mint"] == BONK and p["watchlist"][0]["symbol"] == "BONK"

    other = client(env)
    sign_in(other, env["other"])
    assert other.get("/api/portfolio").json()["watchlist"] == []      # one user never sees another's list

    for _ in range(40):
        r = c.post("/api/watchlist", json={"mint": str(Keypair().pubkey())})
    assert r.status_code == 409
    assert len(c.delete(f"/api/watchlist/{BONK}").json()["watchlist"]) == 29


# ---------- admin ----------

def test_admin_routes_refuse_everyone_else(env):
    anon = client(env)
    assert anon.get("/api/admin/users").status_code == 401
    assert anon.get("/api/admin/users.csv").status_code == 401

    user = client(env)
    sign_in(user, env["user"])
    for path in ("/api/admin/users", "/api/admin/users.csv", f"/api/admin/users/{env['user'].pubkey()}"):
        r = user.get(path)
        assert r.status_code == 403, path
        assert str(env["user"].pubkey()) not in r.text or path.endswith(str(env["user"].pubkey()))


def test_admin_sees_users_snapshots_and_detail(env):
    give(env, env["user"], 2_000_000_000, tokens=[token_account(BONK, 1_000_000)])
    u = client(env)
    sign_in(u, env["user"])
    u.get("/api/portfolio")
    u.post("/api/watchlist", json={"mint": WIF})

    a = client(env)
    r = sign_in(a, env["admin"])
    assert r.json()["isAdmin"] is True
    data = a.get("/api/admin/users").json()
    row = next(x for x in data["users"] if x["wallet"] == str(env["user"].pubkey()))
    assert row["value_usd"] == pytest.approx(321.0)
    assert row["token_count"] == 2 and row["watch_count"] == 1 and row["sign_ins"] == 1
    assert row["is_admin"] is False
    assert "holdings" not in row                            # the list stays light; detail has the coins
    assert data["stats"]["users"] == 2 and data["stats"]["new_24h"] == 2

    last_seen = row["last_seen"]
    env["clock"].t += 3600
    env["net"].wallets[str(env["user"].pubkey())] = (5_000_000_000, {})
    detail = a.get(f"/api/admin/users/{env['user'].pubkey()}?live=1").json()
    assert detail["watchlist"] == [WIF]
    assert detail["user"]["value_usd"] == pytest.approx(750.0)
    assert detail["user"]["last_seen"] == last_seen                     # admin looking is not user activity

    assert a.get(f"/api/admin/users/{Keypair().pubkey()}").status_code == 404


def test_admin_csv_escapes_formulas(env):
    env["net"].symbols[WIF] = '=HYPERLINK("http://x")'    # a coin named like a spreadsheet formula
    give(env, env["user"], 0, t22=[token_account(WIF, 10)])
    u = client(env)
    sign_in(u, env["user"])
    u.get("/api/portfolio")

    a = client(env)
    sign_in(a, env["admin"])
    r = a.get("/api/admin/users.csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0] == "wallet"
    mine = next(row for row in rows[1:] if row[0] == str(env["user"].pubkey()))
    assert "HYPERLINK" in mine[-1]
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@")), cell


# ---------- pages and settings ----------

def test_pages_and_headers(env):
    c = client(env)
    for path in ("/", "/tracker", "/admin", "/assets/wallet.js", "/assets/pansol-coin.jpg"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "noindex" in c.get("/admin").headers["x-robots-tag"]
    assert c.get("/healthz").json() == {"ok": True} and c.head("/healthz").status_code == 200
    assert "max-age" in c.get("/assets/pansol-coin.webp").headers["cache-control"]
    page = c.get("/tracker", headers={"accept-encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    for html in ("/", "/tracker", "/admin"):                  # every image and script a page names exists
        for ref in re.findall(r'(?:src|href)="(/assets/[^"?]+)', c.get(html).text):
            assert c.get(ref).status_code == 200, (html, ref)
    assert c.get("/api/me").headers["cache-control"] == "no-store"
    for path in ("/server.py", "/assets/../server.py", "/store.py", "/state/pansol.db"):
        assert c.get(path).status_code == 404, path


def test_settings_from_env():
    kp = Keypair()
    s = Settings.from_env({"PANSOL_ADMIN_WALLETS": f" {kp.pubkey()} , ", "SOLANA_RPC_URL": "https://rpc.example/?api-key=1"})
    assert s.admin_wallets == {str(kp.pubkey())} and s.rpc_url.startswith("https://rpc.example")
    with pytest.raises(ValueError):
        Settings.from_env({"PANSOL_ADMIN_WALLETS": "nope"})
    with pytest.raises(ValueError):
        Settings.from_env({"PANSOL_PUBLIC_URL": "https://pansol.xyz/path"})
    assert Settings.from_env({}).admin_wallets == frozenset()
    # an external database (free hosts) wins over the disk path the Docker image sets
    pg = "postgresql://u:p@db.example/pansol?sslmode=require"
    assert Settings.from_env({"PANSOL_DB": "/data/pansol.db", "DATABASE_URL": pg}).db_path == pg
    assert Settings.from_env({"PANSOL_DB": "/data/pansol.db"}).db_path == "/data/pansol.db"
    assert Settings.from_env({"DATABASE_URL": f' "{pg}" '}).db_path == pg            # pasted with quotes
    assert Settings.from_env({"DATABASE_URL": "PostgreSQL://u:p@h/db"}).db_path == "PostgreSQL://u:p@h/db"
    with pytest.raises(ValueError):                                                    # never a silent SQLite fallback
        Settings.from_env({"DATABASE_URL": "psql 'postgresql://u:p@h/db'"})


def test_database_outage_answers_503_fast(env):
    from pansol.store import Unavailable

    def down(*a, **k):
        raise Unavailable("connection refused")

    c = client(env)
    sign_in(c, env["user"])
    import pansol.store as store_mod
    original = store_mod.Store.session_wallet
    store_mod.Store.session_wallet = down
    try:
        r = c.get("/api/portfolio")
    finally:
        store_mod.Store.session_wallet = original
    assert r.status_code == 503 and "database" in r.json()["detail"]
    assert c.get("/healthz").status_code == 200
    s = Settings.from_env({"PANSOL_PUBLIC_URL": "https://Pansol.xyz/, https://www.pansol.xyz",
                           "RAILWAY_PUBLIC_DOMAIN": "pansol.up.railway.app"})
    assert s.public_urls == ("https://pansol.xyz", "https://www.pansol.xyz", "https://pansol.up.railway.app")


def test_sign_in_domain_never_comes_from_the_host_header(env):
    """A phishing site relaying our challenge must not get its own domain into the message."""
    c = client(env)
    r = c.post("/api/auth/nonce", json={"wallet": str(env["admin"].pubkey())}, headers={"host": "pansol-airdrop.example"})
    assert r.status_code == 400

    settings = Settings(admin_wallets=frozenset(), db_path=":memory:",
                        public_urls=("https://pansol.example", "https://www.pansol.example"))
    c = TestClient(make_app(settings, store=fresh_store(), chain=Chain("https://rpc.test/"), clock=env["clock"]))
    msg = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())},
                 headers={"host": "www.pansol.example"}).json()["message"]
    assert msg.startswith("www.pansol.example wants you to sign in")
    assert "URI: https://www.pansol.example\n" in msg
    assert c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())},
                  headers={"host": "localhost:8080"}).status_code == 400

    # with nothing configured, only this computer can sign in
    local = TestClient(make_app(Settings(db_path=":memory:"), store=fresh_store(), chain=Chain("https://rpc.test/"),
                                clock=env["clock"]), base_url="http://127.0.0.1:8080")
    msg = local.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())}).json()["message"]
    assert msg.startswith("127.0.0.1:8080 wants you to sign in")


def test_unsigned_challenges_store_nothing(env):
    """Asking for challenges is free for the server, so a flood can't lock anyone out."""
    store = fresh_store()
    app = make_app(Settings(admin_wallets=frozenset({str(env["admin"].pubkey())}), db_path=":memory:",
                            public_urls=("http://testserver",)), store=store, chain=Chain("https://rpc.test/"),
                   clock=env["clock"])
    c = TestClient(app)
    for _ in range(300):
        assert c.post("/api/auth/nonce", json={"wallet": "11111111111111111111111111111111"}).status_code == 200
    with store.db.tx() as t:
        counts = {name: t.one(f"SELECT COUNT(*) AS n FROM {name}")["n"] for name in ("used_nonces", "sessions", "users")}
    assert counts == {"used_nonces": 0, "sessions": 0, "users": 0}
    assert sign_in(c, env["admin"]).json()["isAdmin"] is True


def test_tampered_challenge_is_refused(env):
    c = client(env)
    kp = env["user"]
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    payload, mac = start["challenge"].split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["w"] = str(env["admin"].pubkey())                     # claim to be the admin
    forged = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.b64encode(bytes(kp.sign_message(start["message"].encode()))).decode()
    assert c.post("/api/auth/verify", json={"challenge": f"{forged}.{mac}", "signature": sig}).status_code == 400
    assert c.post("/api/auth/verify", json={"challenge": "garbage", "signature": sig}).status_code == 400


def test_ledger_style_offchain_signature_is_accepted(env):
    import struct
    c = client(env)
    kp = env["user"]
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    msg = start["message"].encode()
    wrapped = b"\xffsolana offchain" + bytes([0, 1]) + struct.pack("<H", len(msg)) + msg
    sig = base64.b64encode(bytes(kp.sign_message(wrapped))).decode()
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code == 200
    # a signature over some other message, even in the envelope, still fails
    other = b"\xffsolana offchain" + bytes([0, 1]) + struct.pack("<H", 5) + b"hello"
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    sig = base64.b64encode(bytes(kp.sign_message(other))).decode()
    assert c.post("/api/auth/verify", json={"challenge": start["challenge"], "signature": sig}).status_code == 401
