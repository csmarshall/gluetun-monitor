"""The DDSketch-style latency histogram: bounded memory, alpha-accurate percentiles.

Why: it backs the all-time latency view. These tests pin the accuracy guarantee
(every percentile within ALPHA relative error), the exact count/avg/min/max,
monotonic percentiles, JSON round-trip, and graceful handling of
empty/garbage input (it feeds the best-effort stats sidecar).
"""

from __future__ import annotations

import math
import random

from gluetun_monitor.histogram import ALPHA, LatencyHistogram


def _true_quantile(values: list[int], q: float) -> int:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def test_percentiles_within_alpha_relative_error() -> None:
    rng = random.Random(42)
    data = [int(rng.lognormvariate(6.5, 0.6)) for _ in range(10_000)]  # skewed, ms-ish
    h = LatencyHistogram()
    for v in data:
        h.add(v)
    for q in (0.50, 0.90, 0.99):
        est = h.quantile(q)
        true = _true_quantile(data, q)
        # The guarantee is relative error <= ALPHA; allow a hair for rounding.
        assert abs(est - true) / true <= ALPHA + 0.01, (q, est, true)


def test_exact_count_avg_min_max() -> None:
    h = LatencyHistogram()
    for v in (100, 200, 300, 400, 500):
        h.add(v)
    s = h.summary()
    assert s["samples"] == 5
    assert s["avg"] == 300  # exact, not bucketed
    assert s["min"] == 100 and s["max"] == 500  # exact


def test_percentiles_monotonic() -> None:
    h = LatencyHistogram()
    for v in range(1, 2001):
        h.add(v)
    s = h.summary()
    assert s["p50"] <= s["p90"] <= s["p99"] <= s["max"]
    assert s["min"] <= s["p50"]


def test_empty_is_zeros() -> None:
    h = LatencyHistogram()
    assert h.quantile(0.9) == 0
    assert h.summary() == {"samples": 0, "avg": 0, "min": 0, "max": 0,
                           "p50": 0, "p90": 0, "p99": 0}


def test_sub_millisecond_clamped() -> None:
    h = LatencyHistogram()
    h.add(0)
    assert h.count == 1 and h.min_ms == 1  # 0 ms clamped to 1


def test_bounded_bucket_count() -> None:
    """A wide range of latencies still uses few buckets (log-scale)."""
    h = LatencyHistogram()
    rng = random.Random(1)
    for _ in range(50_000):
        h.add(rng.randint(50, 10_000))
    assert len(h.buckets) < 120  # ~log_gamma(10000/50) buckets, not 50k samples


def test_json_round_trip() -> None:
    h = LatencyHistogram()
    rng = random.Random(7)
    for _ in range(2000):
        h.add(rng.randint(100, 3000))
    h2 = LatencyHistogram.from_dict(h.to_dict())
    assert h2.count == h.count and h2.min_ms == h.min_ms and h2.max_ms == h.max_ms
    assert h2.summary() == h.summary()


def test_from_dict_tolerates_garbage() -> None:
    assert LatencyHistogram.from_dict("not a dict").count == 0
    assert LatencyHistogram.from_dict([1, 2, 3]).count == 0
    assert LatencyHistogram.from_dict({"buckets": "nope"}).count == 0
    # partial / wrong-typed values degrade to empty, never raise
    assert LatencyHistogram.from_dict({"count": "x", "buckets": {}}).count == 0


def test_from_dict_rejects_count_bucket_mismatch() -> None:
    """#91: a corrupt-but-parseable sidecar whose count disagrees with its buckets
    (e.g. count=100, empty buckets) is rejected to empty rather than loaded — an
    inconsistent sketch would make quantile() seek a rank the buckets can't satisfy."""
    h = LatencyHistogram.from_dict(
        {"count": 100, "sum_ms": 5000, "min_ms": 1, "max_ms": 99, "buckets": {}}
    )
    assert h.count == 0
    assert h.summary()["samples"] == 0


def test_quantile_never_crashes_on_inconsistent_instance() -> None:
    """Defense-in-depth: even a hand-built inconsistent histogram (count>0, no
    buckets) must not turn the quantile fallback's max({}) into a ValueError."""
    broken = LatencyHistogram(count=5, sum_ms=0, min_ms=0, max_ms=0, buckets={})
    assert broken.quantile(0.5) == 0
