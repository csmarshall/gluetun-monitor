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
