# ADR-0009: All-time latency percentiles via a bounded histogram

- **Status:** Accepted
- **Date:** 2026-06-02
- **Relates to:** ADR-0008 (persistent per-site stats — this extends the sidecar)

## Context
ADR-0008 records per-site response latency from a **recent ring** of the last N
successful polls (default 200), and reports avg/min/max + p50/p90/p99 over it. That
answers "how is this site doing **now**" — by design it forgets older samples, so a
site that has been slow all week but is fast in the last 200 polls looks fine, and
there's no lifetime baseline to compare a bad day against.

We wanted an **all-time** latency view too ("how has this site behaved over its
whole life") without unbounded memory. The obvious approaches each fail one
requirement:

- **Keep every sample** → exact percentiles, but unbounded memory and an
  ever-growing sidecar. Out (Tenet 7 — the watchdog must not grow without bound).
- **Just widen the ring** → still a fixed window, still forgets; and a large ring
  is a lot of raw floats to persist every loop.
- **A running percentile from O(1) state** (the old "nudge the estimate" idea / the
  stochastic-quantile and P² estimators) → genuinely O(1), but exact all-time
  percentiles in bounded memory are **impossible**: percentiles are order
  statistics, so unlike the mean (which *does* have an exact O(1) update) you cannot
  recover the new percentile from the old one, the count, and the new sample — the
  answer depends on the distribution's shape around that quantile, which one number
  can't carry. The stochastic estimators approximate, but they're noisy at the
  tails (p99 especially) and slow to react when the distribution shifts (a VPN
  endpoint change) — the worst case for *latency tail* monitoring.

## Decision
Add a small per-site **log-bucketed histogram** (a DDSketch-style sketch;
`histogram.py`) alongside the recent ring, persisted in the same best-effort
sidecar, and surface it as an opt-in "all-time" view in `gluetun-monitor-stats`
(`--lifetime`) and always in `--json`.

How it works: bucket a latency on a logarithmic scale where consecutive bucket
boundaries differ by a factor `gamma = (1+a)/(1-a)`, and represent each bucket by a
single value. That makes every reported percentile **within `a` (relative) of the
true value** — the DDSketch guarantee (Dunning; Ertl). We use `a = 5%`, far tighter
than latency decisions need. Exact `count`/`sum`/`min`/`max` are tracked alongside,
so only the *percentiles* are approximate (avg/min/max stay exact).

Why this over the alternatives:
- **Bounded + sparse.** For latencies spanning ~1 ms to ~100 s, that's a few dozen
  populated `{bucket: count}` entries per site — a few hundred bytes, vs. 200 raw
  floats for the ring. The bucket count grows with the *log* of the value range,
  not with the sample count.
- **Tail-accurate.** The relative-error guarantee holds at p99, unlike the
  stochastic estimators — exactly where latency monitoring cares most.
- **Mergeable.** Bucket counts simply add, so it survives restarts trivially
  (reload = restore counts) and could later merge across containers/sites.
- **Honest.** It complements, not replaces, the ring: "recent" vs "all-time" are
  both shown, and the recent ring stays the default.

The sketch is persisted as `lifetime_latency` (the reloadable counts, bucket keys
as strings) plus a human-readable `lifetime_latency_ms` summary. Like everything in
the sidecar it is best-effort: a malformed `lifetime_latency` degrades to an empty
histogram, never a crash (Tenet 7).

## Consequences
- A second latency lens (lifetime baseline) for spotting chronic-vs-transient
  slowness and "today vs its own normal," at trivial memory cost.
- Percentiles are approximate (≤5% relative); avg/min/max remain exact. Acceptable
  — latency thresholds are not set to 5% precision.
- A new persisted field. Older files simply lack it (reload yields an empty
  histogram that fills going forward); newer files carry it. No migration needed.
- **Decay is intentionally out of scope for now.** The histogram is equal-weight
  all-time. A time-decayed variant (periodically scaling bucket counts so recent
  samples dominate — the "weighted all-time" idea) is a clean future extension that
  this structure supports, but it adds a decay schedule + last-decay bookkeeping we
  don't need yet. If added, it would be opt-in.
