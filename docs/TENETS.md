# gluetun-monitor — Tenets

The principles gluetun-monitor is built on, distilled from the [ADRs](adr/).
The ADRs remain the detailed history and rationale; these tenets are the
front-door statement of *what gluetun-monitor is and why*. When a future
decision is unclear, it should be resolved in favor of these.

---

### 1. Verify the tunnel from inside it.
Health means *traffic actually egresses through the VPN* — so test from **inside
gluetun's network namespace** (`docker exec gluetun wget ...`), never from the
host. A green check that quietly bypassed the tunnel is worse than no check at
all. The public IP we report is read from gluetun's own logs for the same
reason: it reflects the tunnel, not the host. <sub>ADR-0001</sub>

### 2. A broken tunnel is not a sad website.
Distinguish "the VPN path is down" from "a test site is unhappy." A site that
**responds at all** — even 401/403/5xx — proves egress works; only
connection/DNS/TLS failures count as VPN failures. And never act on a single
blip: require repeated, **consecutive** failures across **multiple parallel**
sites before doing anything disruptive. <sub>design policy; see ADR-0003</sub>

### 3. Least privilege by default.
Controlling Docker is root-equivalent, so a network-facing watchdog should not
hold the raw socket. Default to a **locked-down socket proxy** exposing only the
endpoints the monitor needs; the bare socket mount is an explicit opt-in. The
permission surface is allowed to grow only as far as the chosen recovery
strategy actually requires. <sub>ADR-0002</sub>

### 4. Heal the whole stack, in order, only when it'll stick.
Recovery is **ordered and gated**: restore gluetun and **re-verify egress**
*before* touching anything downstream. Don't churn dependents into a
still-broken tunnel, and don't restart on noise. <sub>ADR-0003</sub>

### 5. The dependents are the point — watch them, not just the gateway.
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

### 6. Fail loud; never fake-green.
The cardinal sin is reporting "healthy" while the stack is broken. Prefer an
honest, actionable ERROR over a green light that lies. (Corollary, learned the
hard way: under `set -euo pipefail` a careless parse can crash the loop into a
restart cycle — parse defensively so the watchdog never becomes the outage.)
<sub>ADR-0004; issues #17, #20</sub>

### 7. Over-observe, under-react.
Test broadly and redundantly — from the gateway *and* from every reachable
dependent, connectivity *and* DNS — so a real outage is never missed (Tenet 6).
But gate *action* behind consecutive, threshold-confirmed failure: more vantage
points raise **signal and attribution**, never **trigger-happiness**. A watchdog
that flaps is its own outage (Tenet 2). <sub>ADR-0006; Tenets 2, 6</sub>

### 8. Recovery over prevention — simple and stateless beats clever and fragile.
Failures are inevitable, so optimize for fast, safe **recovery** rather than
elaborate prevention or detection
([Recovery-Oriented Computing](https://en.wikipedia.org/wiki/Recovery-oriented_computing)).
Recovery here is cheap and **non-destructive** (ADR-0005 — volumes preserved),
so the monitor **re-acts rather than remembers**: consecutive in-memory counters,
no persistence, no backoff/circuit-breakers. Treat restart/recreate as the
first-class repair, isolate the fault to the smallest unit (one container), and
accept an occasional extra restart over state that can rot or mislead — a restart
that fixes it beats a clever guess that might not. <sub>ADR-0005, 0006; Recovery-Oriented Computing</sub>

---

Tenet → ADR map: 1→0001 (+0006), 2→(design), 3→0002, 4→0003, 5→0004 (+0006; recreate mechanism: 0005), 6→0004, 7→0006, 8→0005/0006.
