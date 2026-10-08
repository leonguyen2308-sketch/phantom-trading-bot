from fastapi.testclient import TestClient

from phantom.server import make_app


def test_state_and_kill_endpoints():
    fired = []
    app = make_app(lambda: {"mode": "paper", "killed": bool(fired)}, lambda reason: fired.append(reason))
    c = TestClient(app)
    assert c.get("/api/state").json() == {"mode": "paper", "killed": False}
    assert c.post("/api/kill").status_code == 403  # no header: refused
    assert c.post("/api/kill", headers={"x-phantom": "1", "origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/state", headers={"host": "attacker.example"}).status_code == 403
    assert c.post("/api/kill", headers={"x-phantom": "1"}).json() == {"ok": True}
    assert fired and c.get("/api/state").json()["killed"]
    assert c.get("/").status_code == 200
