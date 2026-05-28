# ADR-0003: Recovery is ordered and gated — restore the gateway and verify before touching dependents

- **Status:** Accepted *(extended by ADR-0004)*
- **Date:** 2026-05-28 *(documented retroactively; decision dates to early v1.x)*

## Context
When a connectivity check fails, the naive reaction is to restart everything at
once. That is wrong on two counts: restarting dependents into a tunnel that is
*still* broken just churns them to no effect, and restarting gluetun is itself
disruptive (it drops every dependent's connectivity), so it must only happen on
a real, confirmed failure — not on a transient blip.

## Decision
Failure handling (`handle_failure`, gluetun-monitor.sh:433) is a strict, gated
sequence:

1. **Act only after `FAIL_THRESHOLD` consecutive failures** (`test_all_sites`) —
   no single-blip restarts.
2. **Restart gluetun**, then wait for it to report healthy
   (`wait_for_gluetun_healthy`) and for DNS to stabilize (`wait_for_dns_ready`).
3. **Re-verify connectivity** before going further. Only if egress is actually
   restored do we proceed to restart the dependents
   (`restart_dependent_containers`).
4. If connectivity is **still** failing, do **not** touch the dependents — leave
   them and let the next cycle try a fresh endpoint.
5. Reset the per-site failure counters after a recovery attempt.

## Consequences
- Dependents are only cycled when doing so will actually help; transient blips
  never trigger restarts.
- This codifies the order: gateway first, *verify*, then downstream.
- **The dependent-restart step (step 3) is valid for this flow**, which restarts
  gluetun *in place* (same container ID). Confirmed by the 2×2 test in ADR-0004:
  a dependent recovers via `docker restart` as long as gluetun keeps the same ID
  (A1) — even though a gluetun restart does transiently strand it.
- It does **not** cover a gluetun that is *recreated* by an external actor (new
  ID, e.g. a Watchtower image update), which the monitor doesn't even detect here
  and which `docker restart <dependent>` cannot fix. That trigger and its
  recreate-based recovery are added in ADR-0004 — an extension, not a correction.
