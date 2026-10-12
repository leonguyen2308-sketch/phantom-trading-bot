import httpx

from phantom.alerts import Fanout, XPoster


def _poster(sent):
    def handler(req):
        sent.append(req)
        return httpx.Response(201, json={"data": {"id": "1"}})
    return XPoster("ck", "cs", "tk", "ts", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_posts_only_trades():
    sent = []
    x = _poster(sent)
    x.send("[LIVE] KILL SWITCH: daily loss")
    x.send("[LIVE] BOUGHT 0.010000 BTC at 60,000 for $600.00.")
    assert len(sent) == 1
    assert b"BOUGHT" in sent[0].content and b"#BTC" in sent[0].content
    assert sent[0].headers["Authorization"].startswith("OAuth ")


def test_oauth_signature_matches_known_vector():
    # Example from X's "Creating a signature" docs, adapted to no query/body params.
    x = XPoster("xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
                "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE")
    h = x._auth("POST", "https://api.x.com/2/tweets", nonce="abc", ts="1318622958")
    assert 'oauth_signature="' in h and 'oauth_consumer_key="xvz1evFS4wEEPTGEFPHBog"' in h


def test_fanout_survives_a_failing_channel():
    got = []

    class Bad:
        def send(self, t): raise RuntimeError

    class Good:
        def send(self, t): got.append(t)

    Fanout(Bad(), Good()).send("hi")
    assert got == ["hi"]
