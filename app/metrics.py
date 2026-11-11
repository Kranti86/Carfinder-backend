"""Lightweight in-process metrics (review §"observability").

Not a replacement for a real metrics backend (Prometheus/Datadog) — it's a
zero-dependency way to expose the numbers that matter for this service at
`/metrics`: per-endpoint latency, cache hit rate, upstream call/error counts, and
the last-seen upstream quota. Counters reset on process restart; that's fine for
an at-a-glance operational view and a canary target.

Single-process scope: with multiple dynos each reports its own numbers. Wire the
counters to a shared backend later if you need fleet-wide aggregates.
"""

from __future__ import annotations

from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.latency: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "sum": 0.0, "max": 0.0}
        )
        self.gauges: dict[str, float] = {}

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def observe_latency(self, endpoint: str, ms: float) -> None:
        rec = self.latency[endpoint]
        rec["count"] += 1
        rec["sum"] += ms
        rec["max"] = max(rec["max"], ms)

    def set_gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def snapshot(self) -> dict:
        hits = self.counters.get("cache_hits", 0)
        misses = self.counters.get("cache_misses", 0)
        total = hits + misses
        calls = self.counters.get("upstream_calls", 0)
        errors = self.counters.get("upstream_errors", 0)
        return {
            "counters": dict(self.counters),
            "cache_hit_rate": round(hits / total, 3) if total else None,
            "upstream_error_rate": round(errors / calls, 3) if calls else None,
            "latency_ms": {
                ep: {
                    "count": int(r["count"]),
                    "avg": round(r["sum"] / r["count"], 1) if r["count"] else 0.0,
                    "max": round(r["max"], 1),
                }
                for ep, r in self.latency.items()
            },
            "gauges": self.gauges,
        }


_metrics = Metrics()


def get_metrics() -> Metrics:
    return _metrics
