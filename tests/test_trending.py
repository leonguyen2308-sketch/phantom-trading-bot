import httpx

from phantom.trending import fetch_trending, format_post, run

CA = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
BODY = {
    "data": [
        {"attributes": {"address": "POOL1", "name": "POPCAT / SOL", "market_cap_usd": "1234567", "fdv_usd": "2000000",
                        "volume_usd": {"h24": "3456789"}, "price_change_percentage": {"h24": "45.2"}, "reserve_in_usd": "200000",
                        "base_token_price_usd": "0.5"},
         "relationships": {"base_token": {"data": {"id": f"solana_{CA}", "type": "token"}}}},
        {"attributes": {"address": "POOL2", "name": "SOL / USDC", "volume_usd": {"h24": "9e8"}, "reserve_in_usd": "1e7"},
         "relationships": {"base_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}}}},
    ],
    "included": [
        {"id": f"solana_{CA}", "type": "token", "attributes": {"address": CA, "name": "Popcat", "symbol": "popcat"}},
        {"id": "solana_So11111111111111111111111111111111111111112", "type": "token",
         "attributes": {"address": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL", "symbol": "SOL"}},
    ],
}


def _http():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=BODY)))


def test_parses_ca_mcap_volume():
    c = fetch_trending(_http(), "solana")[0]
    assert (c["ca"], c["symbol"], c["mcap"], c["vol24"]) == (CA, "POPCAT", 1234567.0, 3456789.0)
    text = format_post(c, "#crypto")
    assert f"CA: {CA}" in text and "MC: $1.23M" in text and "Vol 24h: $3.46M" in text and "+45.2%" in text


class Poster:
    def __init__(self): self.posts = []
    def post(self, t): self.posts.append(t)


def test_posts_once_skips_majors_and_repeats(tmp_path):
    p = Poster()
    for _ in range(2):
        run(p, tmp_path / "s.json", network="solana", per_run=5, once=True, http=_http(), clock=lambda: 1000.0)
    assert len(p.posts) == 1 and "POPCAT" in p.posts[0]
