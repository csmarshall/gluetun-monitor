"""Bounded-memory, all-time latency percentiles via a DDSketch-style histogram.

The recent-latency ring (``SiteStat.recent_latencies``) answers "how is this site
doing *now*" over the last N samples. This sketch answers the complementary
"how has it behaved over its *whole life*" — without storing every sample.

Exact all-time percentiles can't be kept in bounded memory (percentiles are order
statistics; you'd need all the data). So we approximate, with a guarantee: values
are bucketed on a logarithmic scale where consecutive bucket boundaries differ by
a factor gamma = (1+a)/(1-a), and each bucket is represented by a single value.
That makes every reported percentile **within `a` (relative) of the true value** —
the DDSketch property (Dunning/Ertl). For latencies spanning ~1 ms to ~100 s at
a = 5%, that's only a few dozen populated buckets per site (a sparse
``{bucket: count}`` dict), it survives restarts, and it's **mergeable** (bucket
counts simply add). Exact ``count``/``sum``/``min``/``max`` are tracked alongside,
so only the percentiles are approximate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Relative accuracy: every reported percentile is within this fraction of the true
# value. 5% is far tighter than latency decisions need, at a few dozen buckets/site.
ALPHA = 0.05
_GAMMA = (1 + ALPHA) / (1 - ALPHA)
_LOG_GAMMA = math.log(_GAMMA)


def _bucket_key(value: int) -> int:
    """Log-scale bucket index for a positive value (ceil(log_gamma(value)))."""
    return math.ceil(math.log(value) / _LOG_GAMMA)


def _bucket_value(key: int) -> int:
    """Representative value for a bucket — the midpoint that bounds the relative
    error of every value in the bucket at ALPHA (2*gamma**k / (gamma+1))."""
    return round(2 * _GAMMA**key / (_GAMMA + 1))


@dataclass
class LatencyHistogram:
    """A sparse log-bucketed histogram of latencies (ms) for all-time percentiles."""

    count: int = 0
    sum_ms: int = 0
    min_ms: int = 0
    max_ms: int = 0
    buckets: dict[int, int] = field(default_factory=dict)

    def add(self, ms: int) -> None:
        """Record one latency sample (ms). Values < 1 ms are clamped to 1 ms
        (sub-millisecond resolution is meaningless for this monitor)."""
        v = max(1, int(ms))
        key = _bucket_key(v)
        self.buckets[key] = self.buckets.get(key, 0) + 1
        if self.count == 0:
            self.min_ms = self.max_ms = v
        else:
            self.min_ms = min(self.min_ms, v)
            self.max_ms = max(self.max_ms, v)
        self.count += 1
        self.sum_ms += v

    def quantile(self, q: float) -> int:
        """Approximate q-quantile (0..1) in ms, within ALPHA relative error; 0 if empty."""
        if self.count == 0:
            return 0
        rank = max(1, math.ceil(q * self.count))  # nearest-rank
        cumulative = 0
        for key in sorted(self.buckets):
            cumulative += self.buckets[key]
            if cumulative >= rank:
                return _bucket_value(key)
        return _bucket_value(max(self.buckets))  # unreachable; defensive

    def summary(self) -> dict[str, int]:
        """samples + exact avg/min/max + approximate p50/p90/p99 (ms)."""
        if self.count == 0:
            return {"samples": 0, "avg": 0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p99": 0}
        return {
            "samples": self.count,
            "avg": round(self.sum_ms / self.count),
            "min": self.min_ms,
            "max": self.max_ms,
            "p50": self.quantile(0.50),
            "p90": self.quantile(0.90),
            "p99": self.quantile(0.99),
        }

    def merge(self, other: LatencyHistogram) -> None:
        """Fold ``other`` into this one (the mergeable property — counts add)."""
        if other.count == 0:
            return
        for key, cnt in other.buckets.items():
            self.buckets[key] = self.buckets.get(key, 0) + cnt
        self.min_ms = other.min_ms if self.count == 0 else min(self.min_ms, other.min_ms)
        self.max_ms = max(self.max_ms, other.max_ms)
        self.count += other.count
        self.sum_ms += other.sum_ms

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly form (bucket keys as strings, since JSON object keys are)."""
        return {
            "count": self.count,
            "sum_ms": self.sum_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "buckets": {str(k): v for k, v in self.buckets.items()},
        }

    @classmethod
    def from_dict(cls, data: object) -> LatencyHistogram:
        """Rebuild from :meth:`to_dict` output; anything malformed yields empty
        (the stats sidecar is best-effort — a bad file must never crash startup)."""
        if not isinstance(data, dict):
            return cls()
        try:
            raw_buckets = data.get("buckets") or {}
            buckets = {
                int(k): int(v)
                for k, v in raw_buckets.items()
                if isinstance(v, int | float)
            }
            return cls(
                count=int(data.get("count", 0)),
                sum_ms=int(data.get("sum_ms", 0)),
                min_ms=int(data.get("min_ms", 0)),
                max_ms=int(data.get("max_ms", 0)),
                buckets=buckets,
            )
        except (ValueError, TypeError, AttributeError):
            return cls()
