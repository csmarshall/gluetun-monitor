# ADR-0015: Per-site role — advisory sites that never gate a restart

- **Status:** Accepted
- **Date:** 2026-07-07
- **Relates to:** ADR-0008 (persistent stats + flaky-site advisory), ADR-0003 (gluetun restart); issues #110, #106

## Context

ADR-0008 named the failure mode: the gluetun root test restarts the tunnel when *any single* site reaches `FAIL_THRESHOLD`, so one flaky indexer can drive a restart storm even while the tunnel is provably up. That ADR deliberately kept the cheap auto-restart and chose to *advise* the operator which site is the troublemaker, and it explicitly **rejected** an automatic circuit-breaker — auto-quarantining a site was judged too opinionated; the operator should decide, informed by data.

Field experience since then surfaced the permanent version of that problem. A site that is unreachable through *every* exit the provider hands out — geo-blocked or anti-VPN, e.g. a torrent indexer — can never succeed regardless of tunnel health. On a live stack this drove roughly 300 gluetun restarts in a day (98% attributed to one such site) with 8 of 9 sites passing every loop; each restart rolled the exit and churned every dependent's connection for nothing. It also fed a distinct alert-lifecycle bug (#106, the `gluetun-unrecovered` false-resolve) because the tunnel kept "failing then recovering" around that one site.

Advising alone leaves only two manual options: tolerate the churn, or delete the site. But there is a real middle case the operator wants — *keep watching whether the site is reachable* (it's a service the stack queries) *without letting its failure roll the tunnel*. ADR-0008 rejected automatic suppression; it did not consider an **explicit, operator-directed** opt-out, which is a different thing.

## Decision

Add an optional per-site **role**, carried on the existing `|key=value` suffix (#60) so one syntax works in `sites.conf` and the `SITES` env alike:

- **`critical`** — the default (a bare URL). Its failure counts toward restarting gluetun, exactly as before. Existing configs are unchanged; this is fully backward-compatible.
- **`advisory`** — the site is still probed and its reachability recorded in the stats, but its failure **never** triggers a restart, and it is excluded from the flaky-site advisory (the operator has already acknowledged it by tagging it).

"Remove" is not a role. It is what the operator does to an advisory site once its reachability stops being worth watching: delete the line. It falls out of the analysis rather than being a config value — advisory *keeps probing* because the reachability signal is the value; a permanently-unreachable site you no longer care about is simply removed.

An unrecognized `role=` value is warned about at startup and falls back to `critical` — fail-closed, so a typo can never silently stop a site from protecting the tunnel. Every site's fully-resolved config (role plus effective timeout/tries) is logged at DEBUG on startup and on each live reload, and a failing advisory site is marked `(advisory)` in the per-loop heartbeat, so the classification is always visible.

Two things are deliberately *not* in this decision. We do not auto-classify a site as advisory from its restart history — that is the automatic suppression ADR-0008 rejected, and the same reasoning holds: the operator decides. And we do not (yet) change the `critical` default from an OR-gate (any one failing restarts) to a quorum. A `health` role plus a restart quorum — for sites that should only trip a restart together, not alone — is a natural follow-up that layers cleanly on top of this and is tracked in #110 as proposal B.

## Consequences

The operator gains an explicit control to stop a known-unreachable site from rolling the tunnel while still watching its reachability — the churn goes away without losing the signal, and without the monitor making the call for them. The default is unchanged and fail-closed, so nothing regresses for existing configs and an unrecognized role still protects the tunnel.

The cost is one more per-site concept to understand, mitigated by the startup/reload logging, the heartbeat annotation, and the docs. The advisory-vs-remove distinction is subtle and is called out explicitly in the README so it does not read as two ways to do the same thing.

Follow-ups: the `health` role + restart quorum (proposal B in #110); and this pairs with #106, which fixed the alert-lifecycle false-resolve that the same churn exposed.
