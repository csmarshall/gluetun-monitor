# gluetun-monitor — Tenets

The principles gluetun-monitor is built on, distilled from the [ADRs](adr/).
The ADRs remain the detailed history and rationale; these tenets are the
front-door statement of *what gluetun-monitor is and why*. When a future
decision is unclear, it should be resolved in favor of these.

---

### 1. First, do no harm — when intent is unclear, choose the option that can't hurt.
A watchdog that damages the system it guards is worse than one that does nothing.
When configuration is **contradictory** or a signal is **ambiguous about *what* to
act on**, resolve toward the **non-destructive** choice: refuse to start on
malformed config rather than guess parameters (fail loud, Tenet 7); **exclude**
over manage when an include and an exclude conflict; **alert** rather than recreate
a container we can't confidently attribute to gluetun; never restart/recreate
against targets the operator didn't choose — where *choose* means **either** an
explicit `DEPENDENT_CONTAINERS` list **or** the deliberate delegation of `auto`
(opting in to let our discovery logic pick). Both are a choice; what's off-limits
is acting on a target *neither* named nor implied by that choice — a guessed
default, or an orphan we can't attribute. This is the companion to Tenet 8:
Tenet 8 governs *when* to act on a confirmed failure; this governs *what* we are
willing to do when we are not sure we are acting on the right thing. Acting on a
guess can restart the wrong container — "I wasn't sure, so I left it alone and said
so" beats that every time. <sub>design policy (v2.0.0); see Tenets 7, 8, 9</sub>

### 2. Verify the tunnel from inside it.
Health means *traffic actually egresses through the VPN* — so test from **inside
gluetun's network namespace** (`docker exec gluetun wget ...`), never from the
host. A green check that quietly bypassed the tunnel is worse than no check at
all. The public IP we report is read from gluetun's own logs for the same
reason: it reflects the tunnel, not the host. <sub>ADR-0001</sub>

### 3. A broken tunnel is not a sad website.
Distinguish "the VPN path is down" from "a test site is unhappy." A site that
**responds at all** — even 401/403/5xx — proves egress works; only
connection/DNS/TLS failures count as VPN failures. And never act on a single
blip: require repeated, **consecutive** failures across **multiple parallel**
sites before doing anything disruptive. <sub>design policy; see ADR-0003</sub>

### 4. Least privilege by default.
Controlling Docker is root-equivalent, so a network-facing watchdog should not
hold the raw socket. Default to a **locked-down socket proxy** exposing only the
endpoints the monitor needs; the bare socket mount is an explicit opt-in. The
permission surface is allowed to grow only as far as the chosen recovery
strategy actually requires. <sub>ADR-0002</sub>

### 5. Heal the whole stack, in order, only when it'll stick.
Recovery is **ordered and gated**: restore gluetun and **re-verify egress**
*before* touching anything downstream. Don't churn dependents into a
still-broken tunnel, and don't restart on noise. <sub>ADR-0003</sub>

### 6. The dependents are the point — watch them, not just the gateway.
gluetun being healthy is necessary but **not sufficient**. Containers that route
through it (auto-discovered by who shares its netns) can be **stranded
loopback-only** — cut down to `lo` — while gluetun itself looks perfect. Health
is **dependent-aware** and
**measured** on each dependent — its netns binding (interface) and its own DNS —
never *inferred* from the gateway (ADR-0006).
Because a shared netns is bound to a container *instance*, recovery keys on
gluetun's **identity**: a `restart` of the dependent suffices while gluetun keeps
the same container ID, but once gluetun is **replaced** (recreated, new ID) the
dependent must be **recreated**, not restarted. <sub>ADR-0004, 0006</sub>

### 7. Fail loud; never fake-green.
The cardinal sin is reporting "healthy" while the stack is broken. Prefer an
honest, actionable ERROR over a green light that lies. (Corollary, learned the
hard way: under `set -euo pipefail` a careless parse can crash the loop into a
restart cycle — parse defensively so the watchdog never becomes the outage.)
<sub>ADR-0004; issues #17, #20</sub>

### 8. Over-observe, under-react.
Test broadly and redundantly — from the gateway *and* from every reachable
dependent, connectivity *and* DNS — so a real outage is never missed (Tenet 7).
But gate *action* behind consecutive, threshold-confirmed failure: more vantage
points raise **signal and attribution**, never **trigger-happiness**. A watchdog
that flaps is its own outage (Tenet 3). <sub>ADR-0006; Tenets 3, 7</sub>

### 9. Recovery over prevention — simple and stateless beats clever and fragile.
Failures are inevitable, so optimize for fast, safe **recovery** rather than
elaborate prevention or detection
([Recovery-Oriented Computing](https://en.wikipedia.org/wiki/Recovery-oriented_computing)).
Recovery here is cheap and **non-destructive** (ADR-0005 — volumes preserved),
so the monitor **prefers to re-act rather than remember**: consecutive in-memory
counters and restart-first, not elaborate state. State has to *earn* its place —
and a few things since have: what Docker forgets across a monitor restart is
persisted deliberately (site stats ADR-0008, alert lifecycle ADR-0012, dependent
memory ADR-0014), and a *provably futile* repeat backs off (the wedged-dependent
case #98) instead of hammering a doomed remediation every loop. The default stays
stateless and backoff-free; each exception is bounded and ADR-recorded, never the
clever-fragile state this tenet warns against. Treat restart/recreate as the
first-class repair, isolate the fault to the smallest unit (one container), and
accept an occasional extra restart over state that can rot or mislead — a restart
that fixes it beats a clever guess that might not. <sub>ADR-0005, 0006, 0008, 0012, 0014; #98; Recovery-Oriented Computing</sub>

---

Tenet → ADR map: 1→(design policy; Tenets 7–9), 2→0001 (+0006), 3→(design), 4→0002, 5→0003, 6→0004 (+0006; recreate mechanism: 0005), 7→0004, 8→0006, 9→0005/0006.
