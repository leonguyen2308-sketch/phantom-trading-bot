import base64
import csv
import io
import json

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
    settings = Settings(admin_wallets=frozenset({str(admin.pubkey())}), rpc_url="https://rpc.test/", db_path=":memory:")
    app = make_app(settings, store=Store(":memory:"), chain=chain, clock=clock)
    return {"app": app, "net": net, "clock": clock, "admin": admin, "user": user, "other": other}


def client(env):
    return TestClient(env["app"])


def sign_in(c, kp):
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())})
    assert start.status_code == 200, start.text
    msg = start.json()["message"]
    sig = base64.b64encode(bytes(kp.sign_message(msg.encode()))).decode()
    return c.post("/api/auth/verify", json={"nonce": start.json()["nonce"], "signature": sig})


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
    r = c.post("/api/auth/verify", json={"nonce": start["nonce"], "signature": forged})
    assert r.status_code == 401
    assert c.get("/api/me").json()["wallet"] is None


def test_nonce_is_single_use_and_expires(env):
    c = client(env)
    kp = env["user"]
    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    sig = base64.b64encode(bytes(kp.sign_message(start["message"].encode()))).decode()
    assert c.post("/api/auth/verify", json={"nonce": start["nonce"], "signature": sig}).status_code == 200
    assert c.post("/api/auth/verify", json={"nonce": start["nonce"], "signature": sig}).status_code == 400

    start = c.post("/api/auth/nonce", json={"wallet": str(kp.pubkey())}).json()
    sig = base64.b64encode(bytes(kp.sign_message(start["message"].encode()))).decode()
    env["clock"].t += auth.NONCE_TTL + 1
    assert c.post("/api/auth/verify", json={"nonce": start["nonce"], "signature": sig}).status_code == 400


@pytest.mark.parametrize("bad", ["", "hello", "0" * 44, "O" * 44, 123])
def test_bad_wallet_address_is_refused(env, bad):
    assert client(env).post("/api/auth/nonce", json={"wallet": bad}).status_code in (400, 422)


@pytest.mark.parametrize("sig", ["", "not base64!!", base64.b64encode(b"x" * 63).decode(), "A" * 300])
def test_malformed_signature_is_refused(env, sig):
    c = client(env)
    start = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())}).json()
    assert c.post("/api/auth/verify", json={"nonce": start["nonce"], "signature": sig}).status_code in (401, 422)


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
    assert p["total_usd"] == 0


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


def test_public_url_pins_the_signed_domain(env):
    settings = Settings(admin_wallets=frozenset(), db_path=":memory:", public_url="https://pansol.example")
    c = TestClient(make_app(settings, store=Store(":memory:"), chain=Chain("https://rpc.test/"), clock=env["clock"]))
    msg = c.post("/api/auth/nonce", json={"wallet": str(env["user"].pubkey())},
                 headers={"host": "evil.example"}).json()["message"]
    assert msg.startswith("pansol.example wants you to sign in")
    assert "URI: https://pansol.example\n" in msg
