"""Metrics collector: counters, latency aggregation, and derived rates."""

from app.metrics import Metrics


def test_cache_hit_rate():
    m = Metrics()
    m.incr("cache_hits", 3)
    m.incr("cache_misses", 1)
    assert m.snapshot()["cache_hit_rate"] == 0.75


def test_upstream_error_rate():
    m = Metrics()
    m.incr("upstream_calls", 10)
    m.incr("upstream_errors", 2)
    assert m.snapshot()["upstream_error_rate"] == 0.2


def test_latency_avg_and_max():
    m = Metrics()
    for ms in (10.0, 20.0, 60.0):
        m.observe_latency("/search", ms)
    lat = m.snapshot()["latency_ms"]["/search"]
    assert lat["count"] == 3
    assert lat["avg"] == 30.0
    assert lat["max"] == 60.0


def test_rates_are_none_when_no_data():
    snap = Metrics().snapshot()
    assert snap["cache_hit_rate"] is None
    assert snap["upstream_error_rate"] is None


def test_gauge_roundtrip():
    m = Metrics()
    m.set_gauge("canary_ok", 1.0)
    assert m.snapshot()["gauges"]["canary_ok"] == 1.0
