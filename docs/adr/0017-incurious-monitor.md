# ADR-0017: The monitor is incurious — it validates structure, not content

- **Status:** Accepted
- **Date:** 2026-07-08
- **Relates to:** ADR-0001 (test from inside the namespace), ADR-0004/0006 (dependent-aware health), ADR-0002 (socket proxy); Tenets 1, 2, 3, 6, 7, 9; issues #36 (foreclosed by this record), #29, #137
- **Contract:** [`docs/COMPATIBILITY.md`](../COMPATIBILITY.md)

## Context

The monitor has always behaved this way; it had simply never said so. A dependent is discovered by *who shares gluetun's network namespace*, never by image or name (Tenet 6). A test site is a reachability oracle — a server answered, therefore egress works — and explicitly *not* something whose health we care about (Tenet 3). The `|role=critical|advisory` distinction added in ADR-0015 is structural ("does this site gate a restart?") rather than semantic ("what kind of site is this?"). Nowhere in the codebase is there a branch that behaves differently because a container happens to be a torrent client, an indexer, or a media server.

Leaving that boundary unstated cost us three things.

First, it left the question of **telemetry** permanently open. An issue proposing opt-in, anonymous adoption analytics (version and platform only, modelled on Home Assistant's) sat in the backlog as a plausible "maybe," because nothing in the project's stated principles ruled it in or out.

Second, and more damaging, it left users **no published statement of what shape their containers must have**. The monitor's actual requirements — a shell for `ls /sys/class/net`, some tool among `wget`/`getent`/`ping` for the resolver check, a `wget` of any flavor in the gateway — were implicit in the code, discoverable only by reading it or by watching the monitor quietly degrade. Someone running a distroless container had no way to learn what they would and would not get.

Third, that same silence let a real bug hide. Establishing whether we could even *publish* "the gateway must provide a `wget`" meant asking what happens when it doesn't — which surfaced #137: a gateway probe whose `exec` could not run was being counted as a *site connectivity failure*, breaching the threshold and restarting the tunnel in a loop that no restart could ever fix. The dependent path had always handled the identical failure correctly, treating an unreadable container as *unevaluated* and leaving it alone. The asymmetry survived precisely because no one had written down what the monitor is entitled to know, and what it must do when it knows nothing.

## Decision

**The monitor validates structure, not content — neither yours nor ours.**

It observes only the structural facts it needs to judge health: that a container shares gluetun's network namespace, that it has interfaces other than loopback, that its resolver can resolve a name, that some server answered a request made from inside the tunnel. It does not know, record, or act upon what you route through the tunnel, which containers you run, what they are for, or what traffic passes between them. Its test sites are opaque oracles: whether something answered, never what it said.

Three consequences follow, and are adopted as part of this decision.

**We owe users an explicit, published contract.** If the monitor asks only for a shape, that shape must be written down. [`COMPATIBILITY.md`](../COMPATIBILITY.md) states it as an *interface rather than a bar*: nothing is unsupported, each capability a container provides unlocks a deeper layer of validation, and a `FROM scratch` binary still gets strand detection. The contract is reference material and will evolve as fallbacks are added; it lives beside this record rather than inside it, so a new degradation path does not require amending an architecture decision.

**When the monitor cannot tell, it must say so and act on nothing.** An unreadable dependent is unevaluated: its alerts are held, not resolved, and it is not remediated. An unprobeable gateway raises a distinct alert and restarts nothing — a restart cannot restore an `EXEC` permission, and a watchdog that damages the system it guards is worse than one that does nothing (Tenet 1; the corollary in Tenet 7). Honest degradation is the price of asking for so little, and it is what makes the ladder in the contract safe to stand on.

**Therefore, no telemetry.** This is not a separate values statement; it is entailed. A monitor whose domain explicitly excludes knowing anything about your setup has no business reporting on it. The proposal in #36 is *foreclosed* by this record rather than rejected on its own merits, and the reasoning generalises: for a tool whose entire purpose is guarding a privacy boundary, phoning home is a contradiction users should not have to audit away. "There is no beacon code" is a categorically stronger guarantee than "the beacon defaults to off," because the latter is a thing every user must read, trust, and keep trusting across releases.

Adoption and efficacy are therefore judged from **coarse public signals that require no trust from anyone**: registry pull counts and the shape of the issue tracker. These are genuinely imprecise — pulls include CI, mirrors, and bots rather than unique users, and a quiet tracker means "no bugs" exactly as plausibly as it means "no users." We accept that imprecision deliberately. The trade being made is coarse public signals over precise private ones, and it is not close.

### What this record does *not* claim

It does not claim total blindness. `endpoint.py` reads gluetun's own logs to report the exit IP, country, and WireGuard server on the `[ENDPOINT]` line. That is observation of *the subject under test* — the tunnel whose health is the entire point — and no restart, remediation, or alert depends on it. Stating this plainly is the difference between a boundary and a slogan.

Nor does it forswear coupling to gluetun. We are named after it and we grep its log format. That coupling is to the **gateway**, not to the operator's setup, and it is confined to exactly one cosmetic module — which makes `endpoint.py` a documented extension point rather than an accident: another VPN gateway satisfying the structural contract is monitored correctly today, and merely reports an unknown endpoint until someone teaches that module its log format.

## Consequences

A class of features is now foreclosed, and can be closed on sight rather than re-litigated: adoption analytics of any kind (#36); content- or traffic-aware probing; per-service health logic ("restart a media server differently"); VPN-provider-specific integrations. Each would require the monitor to know what something *is*, which it has decided not to.

The degradation ladder stops being an apology and becomes a feature. Because the monitor asks so little, it can state exactly what a `FROM scratch` container gets, and mean it. The honest `UNKNOWN` and `UNVALIDATED` verdicts are not gaps in coverage; they are the contract working.

Future design gains a cheap test. Any proposal can be held against the boundary — *does this require knowing what the user runs, or only its shape?* — and #137 shows the cost of not having had one: a documentation question about `wget` uncovered a tunnel-churning restart loop and a fake-green in the post-restart re-verify, both of which had hidden in the space where the boundary should have been written down.

What we give up is real. We will never know how many people run this, on what, or whether a release broke them, except insofar as they tell us. That is the intended shape of the trade, and the alternative — auditing our own beacon on behalf of users who chose a privacy tool — costs more than the data is worth.

One question remains open upstream: whether `wget`'s presence in the gluetun image is part of its supported surface or an internal implementation detail ([passteque/gluetun#3387](https://github.com/passteque/gluetun/discussions/3387)). The answer changes one sentence of the contract and nothing in this decision, because the monitor already requires only *a* `wget`, does not depend on its flavor, and reports rather than restarts when it is absent.
