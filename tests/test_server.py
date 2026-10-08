from fastapi.testclient import TestClient

from phantom.server import make_app


def test_state_and_kill_endpoints():
    fired = []
    app = make_app(lambda: {"mode": "paper", "killed": bool(fired)}, lambda reason: fired.append(reason))
    c = TestClient(app)
    assert c.get("/api/state").json() == {"mode": "paper", "killed": False}
    assert c.post("/api/kill").json() == {"ok": True}
    assert fired and c.get("/api/state").json()["killed"]
    assert c.get("/").status_code == 200
