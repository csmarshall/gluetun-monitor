# Architecture Decision Records

> The distilled principles these records add up to live in
> [`../TENETS.md`](../TENETS.md) — the front-door statement of what
> gluetun-monitor is and why.

Short records of the significant, hard-to-reverse decisions behind
gluetun-monitor — the context, the choice, and the consequences — so the *why*
survives even when the code changes. One file per decision, numbered,
append-only (supersede or amend rather than rewrite history).

Reserved for genuine architecture decisions. Tunable heuristics and
self-documenting implementation details belong in [`../TENETS.md`](../TENETS.md)
or in the code, not here.

Format: [`_template.md`](_template.md). Status ∈ Proposed | Accepted | Superseded.

> **v1.x → v2.0.0 note.** ADRs 0001–0006 were written against the original
> **bash** implementation (`gluetun-monitor.sh`). [ADR-0007](0007-reimplement-in-python.md)
> reimplemented the monitor in **Python** (the `gluetun_monitor` package) for
> v2.0.0. Their *decisions* still hold, but bash-era specifics — `gluetun-monitor.sh:NNN`
> line citations, shell function names (`test_site_async`, `handle_failure`,
> `wait_for_gluetun_healthy`, …), the `docker:*-cli` base image, and the
> `jq`/`curl` approach — describe the v1.x code and now map onto the Python
> package (which talks the Docker API via docker-py). The bash script is retained
> only as the rollback anchor and differential-test oracle.

## Index

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-test-from-inside-namespace.md) | Test connectivity from inside gluetun's network namespace | Accepted (extended by 0006) |
| [0002](0002-socket-proxy-default.md) | Docker socket proxy is the default access method (secure-by-default) | Accepted |
| [0003](0003-ordered-gated-recovery.md) | Recovery is ordered and gated — restore the gateway and verify before touching dependents | Accepted (extended by 0004) |
| [0004](0004-dependent-aware-health.md) | Health is dependent-aware; recovery is conditional on gluetun's identity (restart if same ID, recreate if changed) | Accepted |
| [0005](0005-recreate-mechanism.md) | Recreate mechanism — Docker-API reconstruct, default-on + capability-gated (`AUTO_RECREATE=0` to disable), non-destructive (validated) | Accepted |
| [0006](0006-per-dependent-viability-testing.md) | Per-dependent connectivity + DNS viability testing (gluetun root + one shuffled name per dependent per loop) | Accepted |
| [0007](0007-reimplement-in-python.md) | Reimplement the monitor in Python (v2.0.0) — docker-py seam, characterization + differential no-regressions gate | Accepted |
| [0008](0008-persistent-site-stats-and-advisory.md) | Persistent per-site stats + a flaky-site advisory (keep restart-first; record + advise, don't auto-quarantine) | Accepted |
| [0009](0009-all-time-latency-histogram.md) | All-time latency percentiles via a bounded DDSketch-style histogram (lifetime view alongside the recent ring) | Accepted |
| [0010](0010-notification-layer.md) | Opt-in external notification layer via Apprise (drop-in when unset; best-effort; real-library CI test gates auto-merge) | Accepted |
| [0011](0011-notification-tiers-and-rollup.md) | Notification tiers (`NOTIFY_LEVEL` actionability dial) + per-loop rollup digest | Accepted |
| [0012](0012-alert-lifecycle.md) | Edge-triggered alert lifecycle (announce once, repeat in loops, resolve vs silent-remove) + persisted state | Accepted |
| [0013](0013-release-and-versioning-automation.md) | Release & versioning automation — gate by authorship (ours = human, upstream = auto); release-please + `:edge` + base-digest drift check | Accepted |
| [0014](0014-durable-dependent-memory.md) | Durable dependent memory: persisted gluetun id history + known dependents; adopt-or-warn orphan scan (dead parent in history = provably ours) | Accepted |
| [0015](0015-per-site-role.md) | Per-site role (`\|role=critical\|advisory`, default critical) — advisory sites are probed but never gate a restart; explicit operator opt-out, not auto-quarantine | Accepted |
