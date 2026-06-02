# ADR-0008: Persistent per-site stats + a flaky-site advisory

- **Status:** Accepted
- **Date:** 2026-06-02
- **Relates to:** ADR-0003 (gluetun restart), ADR-0006 (per-loop flow); ROADMAP (notification layer)

## Context
Dogfooding v2 on a real stack overnight surfaced a problem that predates v2 (it's
faithfully inherited from the v1 bash): the gluetun root test restarts gluetun when
**any single site** reaches `FAIL_THRESHOLD` consecutive failures, and
`handle_failure` then **resets all counters** — so there is no memory across
restarts. A single *flaky* test site (an indexer that intermittently times out or
SSL-errors) therefore drives an unbounded **restart storm**: 22 gluetun restarts in
one night → 66 dependent restarts, every one of them triggered by two flaky indexer
sites while the tunnel was provably up (the other 7 sites passed each loop). Each
restart even logged "Recovery complete" because the transient cleared on re-verify,
masking the churn.

Two observations shaped the decision:
- A restart **is** a legitimate cheap fix — a genuinely blocked VPN endpoint is
  often resolved by forcing a new one. So we do **not** want to stop restarting on
  a single-site failure.
- But the monitor has no way to tell the operator *which* site is the chronic
  troublemaker, or that restarts aren't durably helping. The fix the operator
  actually wants is to **prune the flaky site** — they just need to be told.

We considered an automatic circuit-breaker (back off / quarantine a site after N
restarts). It was **rejected** for now: it adds opinionated auto-suppression, and
the operator preferred to keep the cheap auto-fix and decide pruning themselves,
informed by data.

This requires something v2 deliberately avoided (Tenets 8/9 — "re-act rather than
remember; no persistence"): **state that survives a monitor restart.** That stance
was about *fault/backoff* state for cheap, non-destructive dependent remediation;
it does not preclude **observability** data. Recording how sites behave *over time*
is reporting, not control flow, so persisting it does not make recovery stateful or
fragile.

## Decision
1. **Keep restart-first as-is.** A single site breaching `FAIL_THRESHOLD` still
   triggers a gluetun restart (the cheap fix). No auto back-off, no quarantine.
   `FAIL_THRESHOLD` and the per-loop flow are unchanged.
2. **Record persistent per-site stats** in a human-readable JSON sidecar
   (`STATS_FILE`, default `/logs/site-stats.json`), loaded on startup and written
   every loop (a small atomic write). Per site: total polls, total failures (→ rate),
   failure **episodes** (→ average episode length in polls; every failing poll is
   in exactly one episode, so avg = total_failures / episodes), the longest such
   streak, a **failure-reason breakdown** (dns/tls/timeout/connection/http-error/
   other), **response-latency** of successful polls over a bounded ring
   (avg/min/max + **p50/p90/p99** — slowness often precedes failure), restarts
   triggered and **restart-effectiveness** (fraction that actually cleared the
   site on re-verify — distinguishes a site fault from a VPN fault), and
   first-seen / last-failure / last-success timestamps. Plus a bounded
   **recent-restarts** ring buffer for the windowed advisory. The saved JSON also
   includes the computed metrics (rates, percentiles) so it reads at a glance.
   Corrupt/missing file → start fresh, never crash.
3. **Emit a flaky-site advisory.** When one site accounts for a dominant share of
   the gluetun restarts within a recent window — default: ≥ `ADVISORY_MIN_RESTARTS`
   restarts in `ADVISORY_WINDOW`, of which ≥ `ADVISORY_DOMINANCE` fraction are that
   site — log a loud, deduped WARN: *"<site> caused A of the last B restarts over
   the last <window> — it may be flaky; consider reviewing/removing it from
   sites.conf."* This is the human-in-the-loop escalation; when the notification
   layer (ROADMAP) lands, it fires there too.

## Consequences
- **The operator gets attribution + history**, not just symptoms: "this site keeps
  being flaky," and "which sites cause the restarts, how often." That's the data
  needed to prune a bad test site.
- **The churn is *not* auto-stopped** — by deliberate choice. Until the operator
  prunes the flagged site, restarts continue (the advisory exists to prompt that
  quickly). A future automatic back-off remains possible if this proves
  insufficient (it would be a new, opt-in decision).
- **We relax the stateless stance (Tenets 8/9) for observability only.** Recovery
  control flow stays in-memory and reset-on-restart; only the *stats* persist.
  Persistence is best-effort: a missing/corrupt/unwritable stats file degrades to
  in-memory and never blocks the monitor (Tenet 7 — the watchdog must not become
  the outage).
- **New surface:** a writable stats path (the existing `/logs` mount by default)
  and the advisory knobs. JSON (not SQLite) keeps it dependency-free, greppable,
  and easy to inspect; a heavier store is a future option if richer queries are
  needed.
