# ADR-0016: Escalate a wedged dependent — distinct alert + capped remediation backoff

- **Status:** Accepted
- **Date:** 2026-07-07
- **Relates to:** ADR-0005 (recreate mechanism), ADR-0012 (alert lifecycle); TENETS.md Tenet 9; issue #98

*(Backfilled: #98 shipped in 2.2.4. The decision changed core remediation semantics and added a config contract, so it earns a record.)*

## Context

The remediation loop retries a failed dependent every loop — correct for the common case, where the next loop's retry clears a transient failure. But some failures **cannot** self-heal by retrying. The canonical one, seen repeatedly on the live stack, is an unremovable parked twin left by an interrupted recreate: the storage driver refuses to remove it (`dataset is busy`) because a force-killed container's process tree survived and pinned the mount. Retrying the removal is free but futile, and it looks **identical** every loop.

Two problems followed. First, the operator got only the generic `dependent unhealthy` alert — no signal that this was a stuck, human-required state rather than a transient the monitor would clear, and no guidance on what to do. Second, the monitor hammered the daemon with the same doomed removal every 30 s indefinitely (observed: ~1400 futile attempts over 14 h), which is pure noise against the daemon and the logs.

This bumps against Tenet 9 ("re-act rather than remember; no backoff/circuit-breakers"). That stance is about not accreting fragile fault-state for the *cheap, self-healing* path — it does not require blindly repeating a provably-futile action forever. A bounded backoff on a repeat that is *known* not to be helping is not the clever-fragile state the tenet warns against.

## Decision

When the **same** remediation failure repeats `WEDGE_ESCALATE_AFTER` consecutive times (default 3), declare the dependent **wedged**:

1. **Escalate the alert.** The generic `dependent-unhealthy` alert is *superseded* (ADR-0012 — a silent retire, not a false resolve) by a distinct `dependent-wedged` alert carrying the exact driver error, the parked twin's inspect state, and an operator **runbook** (locate the holder via mountinfo, kill only cgroup-confirmed pids, `docker rm -f`). The alert alone is enough to act on.
2. **Back off the retry.** The remediation delay doubles per failed attempt, capped at `WEDGE_BACKOFF_CAP` seconds (default 600; `0` = no backoff), instead of attempting every loop. **Probing never stops** — the dependent is still checked every loop, so recovery (operator clears the blocker, or the container recovers on its own) is detected promptly and the monitor finishes the heal itself.
3. **A changed failure signature restarts the count.** A *different* error is a new situation, not a deeper wedge, so escalation and backoff reset.

Restart-safe: the `dependent-wedged` alert persists in the sidecar (ADR-0012), so a monitor restart mid-wedge re-recognizes the escalated state via `AlertState.is_active` rather than re-announcing from scratch. Two config knobs (`WEDGE_ESCALATE_AFTER`, `WEDGE_BACKOFF_CAP`) are now part of the contract.

## Consequences

The operator gets a distinct, actionable page exactly when a dependent needs hands-on intervention — and stops getting a doomed remediation hammered at the daemon every loop in the meantime. Recovery is still detected within one loop because probing continues.

The cost is the Tenet 9 carve-out (amended there) and a small amount of per-dependent wedge bookkeeping — bounded, reset on a changed signature, and reconstructable from the persisted alert after a restart, so it is not the rot-prone state the tenet guards against. This is deliberately **not** a general circuit-breaker: only a provably-futile *repeat* backs off, and only for the wedged case; the cheap self-healing path is untouched.
