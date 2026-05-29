# ADR-0004: Health is dependent-aware; recovery is conditional on gluetun's identity

- **Status:** Accepted
- **Date:** 2026-05-28
- **Extends:** ADR-0003 (adds a new failure trigger + a conditional recovery; does not invalidate it)
- **Recreate mechanism:** the ID-changed branch's *how* is deferred to ADR-0005
- **Testing model:** the dependent connectivity + DNS verification is ADR-0006

## Context
Issue #20. Dependents typically use `network_mode: service:gluetun`, so they
share gluetun's network namespace — and that share is bound to a specific
container **instance**, not to the name. When gluetun is **recreated** (e.g. a
Watchtower image update destroys the old container and creates a new one with a
new ID), the dependents are left **stranded loopback-only**: they keep only `lo`
and lose `eth0`/`tun0`, while the new gluetun is freshly healthy. ("Stranded
loopback-only" is our name for this exact state — a `Running` container whose
only interface is `lo`.)

Because the monitor tests connectivity *only from inside gluetun* (ADR-0001),
every site check passes and the monitor reports healthy while the dependents are
cut off from the network entirely. This is the core complaint: the watchdog
reports green while the stack is broken (violates Tenet 7).

We validated the mechanics empirically on rosa (Docker 29.1.3,
`issue20-netns-experiment.sh`): Q1 a dependent's `HostConfig.NetworkMode` is
stored as the **resolved full container ID** (`container:<id>`), not the name;
Q2 even a *same-ID* `docker restart gluetun` rebuilds the netns and strands
dependents (`lo` only); Q3 a recreate (new ID) strands them while they stay
`Running` with a `NetworkMode` pointing at the dead old ID; Q4 `docker restart
<dependent>` then fails hard (`No such container: <old-id>`, `Exited(137)`); Q5
only recreating the dependent restores it. A follow-up
(`issue20-restart-recovery-test.sh`) closed the 2×2 of stranding-cause ×
recovery-action:

| stranding cause | `docker restart <dep>` | recreate `<dep>` |
|---|---|---|
| gluetun **restart** (same ID) | **recovers** (A1) | recovers (A2) |
| gluetun **recreate** (new ID) | **fails** (B1) | recovers (B2) |

So restart-the-dependent is a valid recovery **exactly when gluetun keeps the
same container ID** (the dependent re-joins gluetun's rebuilt netns); once the ID
changes, the dependent's `NetworkMode` points at a dead container and only a
recreate works. The container **ID** is therefore both the detection signal and
the recovery-branch selector.

**Dependents are not a monolith.** A netns rebuild strands *all* of them at once,
but they can **diverge**: compose's `depends_on: { condition: service_healthy,
restart: true }` (which the #20 reporter has on one dependent) or mixed tooling
can re-bind some dependents to the new gluetun and leave others stranded. State
must therefore be evaluated **per dependent**, not as a single global flag.

## Decision
1. **Health must be dependent-aware — measured, not inferred.** The monitor
   detects a stranded dependent and stops reporting healthy. Per dependent, in
   order of authority:
   - **Direct (primary) — interface check:** `docker exec <dep> ls /sys/class/net`
     — a dependent showing only `lo` is **stranded loopback-only**. This is
     ground truth and catches it regardless of cause. (Connectivity + DNS
     verification is layered on top in ADR-0006.)
   - **Inspect (pre-filter, branch-selector, fallback):** compare **each**
     dependent's `NetworkMode` target-ID to gluetun's current **ID**. This cheaply
     flags *suspect* dependents, **selects the recovery branch** (below), and is
     the **fallback** when a dependent can't be exec'd (distroless/scratch).

     > **Implementation note (v2.0.0):** this ADR originally also proposed
     > tracking gluetun's `.State.StartedAt` across cycles. The v2 build does
     > **not** — it tracks the current id only. `StartedAt` would detect a
     > *same-id in-place restart*, but the **interface check is ground truth every
     > loop** and already detects the resulting strand from *any* cause (same-id
     > restart *and* recreate both strand dependents to `lo`, per Q2/Q3), while the
     > id comparison selects restart-vs-recreate. `StartedAt` would only duplicate
     > detection the interface check already does, so we dropped it rather than
     > carry extra cross-cycle state (Tenets 8/9 — simple/stateless). Likewise the
     > "catches a strand that predates the monitor's own startup" claim holds only
     > for *discovered/known/explicitly-listed* dependents — a recreate-strand
     > pre-dating startup needs an explicit `DEPENDENT_CONTAINERS` (see README).
     >
     > What v2 *does* keep across cycles is a **remembered-dependent set**: the
     > union of everything discovered (or listed) so far, pruned to containers
     > that still exist. This is what lets a dependent stay tracked after gluetun
     > is recreated under it (its `NetworkMode` now points at the dead old id, so
     > current-id discovery no longer matches it — but we remember it). It is
     > *discovery* memory, not failure/backoff state: it carries no counters, and
     > a monitor restart resets it — so it stays within Tenet 8's "re-act rather
     > than remember" (which is about not persisting *fault* state).
2. **Recovery is conditional on gluetun's identity — per dependent, not a blanket
   recreate.** Read the stranded dependent's `NetworkMode` (`container:<X>`) and
   compare `<X>` to gluetun's current `.Id`:
   - `<X>` is an ID **== current** (same-instance bounce — incl. the monitor's own
     ADR-0003 restart) → **`docker restart <dependent>`** rejoins (A1). Cheap, no
     new permissions.
   - `<X>` is an ID **!= current** (gluetun replaced, new ID) → **recreate** the
     dependent (B1/B2); `NetworkMode` is immutable, so there is no in-place fix.
     We *know* restart fails here, so go straight to recreate — avoids a
     guaranteed-fail restart and the `Exited(137)` churn from Q4.
   - `<X>` is a **name** (some setups write `network_mode: container:<name>`;
     Docker may store the name rather than an ID — untested) → **try restart
     first, escalate to recreate on failure**: safe (a failed restart just stops
     it, which recreate then fixes) and sidesteps the name-resolution unknown.
   After acting, **verify each dependent** (running + has a non-`lo` interface).
   Confirmation cadence is ADR-0006's reaction model; the recreate *mechanism* is
   ADR-0005.
3. **Prefer direct measurement over inference.** #20 happened because stack
   health was inferred from the gateway; the fix is to *measure* each dependent —
   the interface check always, plus connectivity + DNS via ADR-0006 — and fall
   back to inspect inference only where exec isn't possible.

## Consequences
- The monitor stops lying about health — the central fix for #20.
- Detection is per-dependent and uses `docker exec` (already required by
  ADR-0001) plus inspect; **no new permission class**. Inspect alone is the
  fallback for non-exec'able dependents, and divergent or newly-added dependents
  are handled correctly.
- **ADR-0003 stands.** Its restart-based recovery is confirmed correct for the
  monitor's own (same-ID) gluetun restart — A1. This ADR extends it with the
  external-recreate trigger and the recreate branch.
- The **cheap branch** (matching ID → restart dependents) can ship immediately
  with detection, reusing ADR-0003's existing mechanism and needing no new
  permissions.
- The **recreate branch** needs the dependent's full run spec, which the monitor
  does not hold — choosing *how* to recreate is a real, separable decision and is
  deferred to **ADR-0005**. Until it lands, the minimum bar is accurate
  dependent-aware health + an actionable alert + the cheap restart branch; the
  monitor must not silently paper over a dependent stranded by a recreate.
- The active per-dependent connectivity + DNS probe is a decided part of the
  design — **ADR-0006** (distributed, attributed testing). It is on by default; a
  dependent that fails it is routed back into this ADR's recovery branch.
- Supersedes the implicit assumption — present in the original design — that a
  healthy gluetun implies a healthy stack.
