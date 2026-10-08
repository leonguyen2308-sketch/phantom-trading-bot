"""Runs TypeSafeJev through the real TypeSafe SDK with a mocked HTTP transport (no network)."""
import json

import pytest

httpx2 = pytest.importorskip("httpx2")

from phantom.config import load_strategy  # noqa: E402
from phantom.features import SNAPSHOT_FIELDS  # noqa: E402
from phantom.jev import TypeSafeJev  # noqa: E402
from tests.test_jev import ANSWERS  # noqa: E402


def test_round_trip_through_sdk_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert set(body["questions"]) == set(ANSWERS)
        assert body["state"]["snapshot"]["ret_1"] == 0.1
        answers = {k: {**v, "legend": {"0": "a"}, "probabilities": {"0": 1.0}} if v["type"] == "score" else v for k, v in ANSWERS.items()}
        return httpx2.Response(200, json={"model": "jev-test", "usage": {"input_tokens": 100, "output_tokens": 6}, "answers": answers})

    jev = TypeSafeJev(load_strategy("config/strategy.yaml"), cache_path=tmp_path / "c.sqlite")
    jev.client._http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    snap = {k: 0.0 for k in SNAPSHOT_FIELDS} | {"ret_1": 0.1}
    view, raw, _ = jev.ask(snap)
    assert view.tp == 0.62 and view.regime_up == 0.6
    jev.ask(snap)  # second time comes from the cache
    assert len(calls) == 1
