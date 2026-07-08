# Compatibility — what the monitor needs to see

gluetun-monitor is deliberately **incurious** ([ADR-0017](adr/0017-incurious-monitor.md)). It does not know or care *what* you route through the tunnel, *which* containers you run, or *what* traffic flows through them. It cares only that they have a shape it can measure health against.

This page states that shape exactly. It is not a bar to clear. It is an **interface**: provide more of it and you unlock deeper validation, provide none of it and you still get watched. Nothing on this page is "unsupported" — a `FROM scratch` container behind the tunnel is a first-class citizen of the parts of it the monitor can reach.

---

## What a dependent *is*

The entire definition:

```
HostConfig.NetworkMode == "container:<gluetun>"
```

A dependent is any container sharing gluetun's network namespace. That's it. Not its image, not its name, not its purpose. The monitor matches the container-name form, the full-id form, and the short-id prefix, so however you wrote it in your compose file, it is found.

Everything downstream follows from that one structural fact: because a dependent shares gluetun's already-proven egress, the only fault that can be *uniquely its own* is its DNS resolver. That is why the checks below look the way they do.

---

## What you get for what you provide

Each capability your container ships unlocks a deeper layer of validation. None of them are required.

| Your container provides | The monitor can | What you get |
|---|---|---|
| *nothing* (`FROM scratch`) | read `HostConfig.NetworkMode` via the Docker API | **Strand detection.** If gluetun is *recreated* (new container id), your netns parent is dead and you are cut off. The monitor sees the id has moved and recreates you. Works on a single static binary. |
| **+ a shell** | `ls /sys/class/net` | **Direct interface check.** The monitor sees your actual interfaces — `eth0`/`tun0` (live) versus loopback-only (stranded) — and detects a strand without inferring it from container ids. |
| **+ any one of `wget`, `getent`, `ping`** | resolve a hostname from inside your container | **Full L7 viability.** The monitor validates *your own resolver* — the one fault that is uniquely yours. This is the deepest check available. |

**First-class = a shell + any one of those three tools.** No particular distro. No particular `wget`.

### `wget` flavor does not matter

Probe results are classified on the **HTTP response**, never on `wget`'s exit code. GNU `wget` reports HTTP errors as exit 6/8; BusyBox `wget` returns exit 1 for the same responses. Keying on "did a server answer?" makes both correct, and makes a harmless `404` from a BusyBox dependent read as the success it is. Bring whichever you have.

### The DNS cascade

The resolver check tries, in order, `wget` → `getent hosts` → `ping`, stopping at the first tool that actually exists. If none of them do, the verdict is **`UNVALIDATED`** — and the monitor says so out loud and falls back to the interface check. It does not guess.

---

## When the monitor can't tell, it says so

This is the load-bearing promise, and it is worth stating plainly because it is what makes the ladder above safe to stand on:

- A dependent whose interfaces can't be read is `UNKNOWN` → **unevaluated**. Its alerts are *held*, not resolved, and it is **not remediated**. *"I wasn't sure, so I left it alone and said so."*
- A dependent with no usable DNS tool is `UNVALIDATED` → the monitor reports it and relies on the interface check rather than inventing a verdict.
- If the **gateway** itself can't be probed — no `EXEC` permission, an unreachable proxy, a missing `wget` — the monitor raises a distinct `cannot probe` alert, holds every active alert, and **restarts nothing**. A restart cannot restore an EXEC permission, and a watchdog that damages the system it guards is worse than one that does nothing ([#137](https://github.com/csmarshall/gluetun-monitor/issues/137)).

The monitor never reports healthy on absence of evidence. Fail loud; never fake-green.

---

## Making a container first-class

If you are building or choosing an image to run behind the tunnel and want the deepest validation, ship:

1. **A shell** — so `ls /sys/class/net` can run. Any `sh` will do.
2. **One DNS tool** — `wget`, `getent`, or `ping`. Most base images already have at least one; BusyBox alone satisfies it.

That is the whole checklist. If you can't (distroless, scratch, a hardened runtime), you lose the deeper checks and keep strand detection — and the monitor will tell you which layer it's operating at, every loop, rather than pretending.

---

## The gateway

gluetun is the only gateway that is tested, but the coupling is **structural, not brand-specific**. A gateway must be:

- **exec-able**, with a shell and a `wget` on `PATH` (any flavor), and readable interfaces at `/sys/class/net`
- reachable through the Docker API with `CONTAINERS`, `POST`, and `EXEC` (see below)

Note the honest caveat: gluetun installs GNU `wget` deliberately, but that is an *implementation detail* of its image rather than a documented guarantee. Whether it forms part of gluetun's supported surface is [an open question upstream](https://github.com/passteque/gluetun/discussions/3387). The monitor is built not to depend on the answer — it requires *a* `wget`, doesn't care which, and if one ever vanishes it reports rather than restarts.

The **only** gluetun-specific code in the project is `endpoint.py`, which greps gluetun's own logs for the exit IP, country, and WireGuard server to print the `[ENDPOINT]` line. That is cosmetic reporting about *the subject under test*, not part of any health decision — no restart, no remediation, no alert depends on it. It is therefore a documented **extension point**: point the monitor at a different VPN gateway that satisfies the structural contract above and health monitoring works unchanged; teach `endpoint.py` that gateway's log format and you get the endpoint line too.

---

## Docker API surface

The monitor needs three capabilities, and no more. With the recommended [socket proxy](../README.md#docker-socket-proxy):

| Setting | Required for |
|---|---|
| `CONTAINERS=1` | list, inspect, and read container logs; create the replacement when recreating a stranded dependent |
| `POST=1` | restart / remove / start containers (the recreate path rides the same flag) |
| `EXEC=1` | run the connectivity and interface probes inside gluetun and the dependents |

`POST=1` is unavoidable, not laziness: tecnativa's `POST` is a *binary* switch over the container API. With `POST=0` the `EXEC` and `ALLOW_RESTARTS` carve-outs are inert — neither probing nor restarting works — and with `POST=1` the whole container write surface is open, including create and remove. There is no configuration that grants exec and restart while denying recreate. This was established empirically rather than assumed ([#29](https://github.com/csmarshall/gluetun-monitor/issues/29)).

Nothing else is enabled: no `IMAGES`, `NETWORKS`, `SERVICES`, `SWARM`, or `SYSTEM`.

---

## Platforms

Published for `linux/amd64`, `linux/arm64`, and `linux/arm/v7`. Containers are assumed to be Linux — the monitor relies on network-namespace semantics and `/sys/class/net`.

---

## What the monitor will never ask of you

Because it is incurious, there are whole categories of requirement it does not have and will not grow ([ADR-0017](adr/0017-incurious-monitor.md)):

- It never inspects your traffic, nor knows what you use the tunnel for.
- It has no per-service knowledge. There is no code path that behaves differently because a container is a torrent client, an indexer, or a media server — and there never will be.
- It has no VPN-provider-specific logic.
- It does not phone home. There is no telemetry, opt-in or otherwise, and no code to audit for it.

Your test sites are opaque reachability oracles: the monitor cares *only* whether something answered, never what it said or who it belongs to.
