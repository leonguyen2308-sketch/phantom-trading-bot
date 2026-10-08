from dataclasses import replace

from phantom.features import SNAPSHOT_FIELDS, build_snapshot, snapshot_hash


def test_snapshot_uses_only_closed_candles_before_decision(candles):
    t = candles[300].start + candles[300].seconds  # decision at close of candle 300
    snap = build_snapshot(candles, t)
    assert snap is not None
    # Same snapshot when the future is cut off entirely
    assert build_snapshot(candles[:301], t) == snap


def test_changing_the_future_does_not_change_the_snapshot(candles):
    t = candles[300].start + candles[300].seconds
    before = build_snapshot(candles, t)
    tampered = list(candles)
    for i in range(301, len(tampered)):
        tampered[i] = replace(tampered[i], close=tampered[i].close * 3, high=tampered[i].high * 3)
    assert build_snapshot(tampered, t) == before


def test_candle_still_open_at_decision_time_is_ignored(candles):
    t = candles[300].start + 600  # candle 300 is only 10 minutes old
    snap = build_snapshot(candles, t)
    assert snap == build_snapshot(candles[:300], t)


def test_snapshot_is_numeric_and_has_no_prices_or_dates(candles):
    t = candles[300].start + candles[300].seconds
    snap = build_snapshot(candles, t)
    assert set(snap) == set(SNAPSHOT_FIELDS)
    assert all(isinstance(v, (int, float)) for v in snap.values())
    # Normalized values only: nothing on the scale of a BTC price or a unix timestamp
    assert all(abs(v) < 1_000 for v in snap.values())


def test_not_enough_history_returns_none(candles):
    t = candles[20].start + candles[20].seconds
    assert build_snapshot(candles, t) is None


def test_hash_is_stable_and_order_independent(candles):
    t = candles[300].start + candles[300].seconds
    snap = build_snapshot(candles, t)
    assert snapshot_hash(snap) == snapshot_hash(dict(reversed(list(snap.items()))))
