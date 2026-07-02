# ADR-0014: Durable dependent memory and id-history adoption

- **Status:** Accepted
- **Date:** 2026-07-02

## Context

The monitor's knowledge of its dependents lived in two places, and both fail at
once. Auto-discovery matches `NetworkMode == container:<CURRENT gluetun id>`
among **running** containers; the remembered set (ADR-0004) covers dependents
stranded on an old id — but it was in-memory only, populated by the first
completed dependent phase.

A monitor restart followed closely by a gluetun recreate — one
`docker compose up -d` can produce both — therefore blinded remediation
permanently: the fresh monitor's memory was empty, the stranded dependents
pointed at the dead old id (and were usually driven to Exited by their own
restart policies, since a start onto a dead netns id fails hard), and discovery
never matched them again. Observed live during the issue #76 dogfood, twice in
one evening, in both forms (running-stranded and exited-stranded); the stack
stayed down ~80 minutes until healed by hand (#97).

The dangling-orphan scan already *found* these containers but could only warn:
a dead parent id could not be confirmed as gluetun, and adopting someone else's
container would violate Tenet 1.

Designs considered and rejected:

- **Threshold heuristics** ("if X% of last-seen dependents are missing…"):
  needs tuning, misfires at small N, and fails in both directions (intentional
  decommission → false alarm; a single stranded dependent → under threshold).
- **Freshness gating / host boot-id in the state file**: Docker container ids
  are 256-bit random and never reused, so a stale entry cannot become a false
  positive — there is no id-reuse window for a boot epoch to guard. A reboot
  merely converts running-stranded into exited-stranded, which is already the
  adoption case. Stale state can only mean *missing* knowledge, which degrades
  to the pre-#97 behavior — never wrong action.
- **Persisting an id→containers mapping**: the association already has an
  authoritative owner — each container's own `NetworkMode`, read live each
  loop. A copy would need per-loop maintenance and could drift; and requiring
  "name in our records" as an adoption condition would re-introduce the memory
  race being fixed. The id alone is the strongest possible evidence.
- **Fixed-size id history ("last N")**: a recreate storm could evict an id
  that a long-exited dependent still references, and adoption would miss it.

## Decision

Persist **only what Docker forgets**, in a `monitor-state.json` sidecar
(`MONITOR_STATE_FILE`, default `/logs/monitor-state.json`; atomic
tmp+rename+fsync writes, write-on-change; a corrupt or missing file degrades to
empty memory — state must never take monitoring down):

- **`gluetun_ids`** — every container id gluetun has been seen under, newest
  first. Pruned **by reference**: an id is dropped only when no existing
  container's `NetworkMode` points at it, so an entry lives exactly as long as
  it can matter to an adoption decision.
- **`known_dependents`** — the ADR-0004 remembered set, seeded back into
  memory at startup and mirrored after each prune.

The orphan scan becomes **adopt-or-warn**, runs at startup and every loop, and
inspects **all** containers including exited ones:

- Dead parent id **in** the history → the container provably shared our
  gluetun's netns (ids are never reused) → adopt into the managed set and
  remediate through the normal path (`AUTO_RECREATE` gate applies).
- Dead parent id **not** in the history → the pre-existing behavior: a
  once-per-name warning suggesting `DEPENDENT_CONTAINERS` (running containers
  only; an exited container on an unknown dead parent is ordinary leftover
  junk, not a page).

`schema_version` and `updated_at` are observability only — never decision
inputs.

## Consequences

- The restart+recreate window is closed: a stranded dependent is adopted and
  healed on the monitor's first loop instead of staying invisible. Replayed
  against the observed incident, downtime drops from ~80 minutes (manual) to
  one loop.
- A second persistent sidecar joins site-stats and notify-state. Same contract
  (best-effort, atomic, corrupt-tolerant), so the operational surface is
  familiar; the file is human-readable for debugging ("why didn't it adopt?").
- The monitor now acts on containers it never saw in-process, on the strength
  of the persisted id history. The safety argument rests entirely on Docker
  ids being unique forever; the negative path (unknown dead parent → warn
  only) is pinned by tests.
- Bootstrap gap, accepted: a first-ever run (empty history) against
  already-stranded dependents cannot confirm parentage. The documented
  workaround remains an explicit `DEPENDENT_CONTAINERS` list.
- The per-loop scan now lists and inspects all containers (including exited)
  rather than running ones only — a handful of extra inspects per loop through
  the socket-proxy, accepted for the coverage.
