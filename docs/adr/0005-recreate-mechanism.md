# ADR-0005: Recreate mechanism — Docker-API reconstruct, default-on (capability-gated)

- **Status:** Accepted
- **Date:** 2026-05-28
- **Refines:** ADR-0004 (the ID-changed recovery branch)

## Context
ADR-0004 establishes that when gluetun is **recreated** (new container ID), a
**stranded loopback-only** dependent must be **recreated** — restart can't rejoin
(B1) and `NetworkMode` is immutable. Issue #20's own *expected behavior* asked
for exactly this: the monitor should force a recreation of dependents stuck on
`lo`. So auto-recreate is the **original request**, not a nice-to-have.

Why "recreate" and not "update the network in place": `NetworkMode` is
**immutable** on a live container — `docker update` changes only
cpu/memory/restart-policy, and a `container:`-mode container can't have its
network swapped. The only in-place route is hand-editing the daemon's on-disk
`hostconfig.json` and restarting `dockerd` (host-root, unsupported) — out of
scope for a socket-only watchdog. So changing a dependent's netns target means
**create a new container object** with the same volumes + corrected `NetworkMode`.

Two constraints pull against doing it blindly:
- The monitor holds only a Docker connection (socket proxy, ADR-0002) and **no run
  spec** for the dependents.
- Recreate is a **behavioral change**, not a data-wipe — *if done carefully*.
  Named volumes and bind mounts are always preserved (not owned by the
  container); **anonymous** volumes are preserved too **iff** we `docker rm`
  **without `-v`** and re-attach them **by name** on the new container (copy the
  `Mounts`). The only unavoidable loss is the container's ephemeral **writable
  layer** (non-volume in-container files), which is ephemeral by design. What
  *does* change: the container gets a **new ID**, there is **brief downtime**
  during the swap, and any **reconstruction-fidelity bug** could silently drop a
  setting. That residual surprise (a watchdog recreating containers) is handled by
  a **prominent call-out** plus an `AUTO_RECREATE=0` escape hatch — not by
  defaulting off, since recovery is the tool's job and the operation is
  non-destructive.

**Validated** (`issue20-recreate-dataloss-test.sh`, Docker 29.1.3): with `Mounts`
copied and `rm` run **without `-v`**, named **and anonymous** volume data survive
a recreate; only the ephemeral writable layer is lost. A naive recreate that skips
re-attaching the anonymous volume *does* lose it — so copying `Mounts` is mandatory.

So the *mechanism* and its *rollout* are one decision. Three mechanisms were
considered:
- **A — detect + alert only:** accurate health + a loud ERROR naming the stranded
  dependents and the exact `docker compose up -d --force-recreate <svc>` fix. No
  new permissions/coupling.
- **B — reconstruct via the Docker API:** `inspect` the dependent → set
  `HostConfig.NetworkMode` to the current gluetun → **strip the netns-conflicting
  fields** (`Hostname`, `Domainname`, `ExposedPorts`, `MacAddress`, port bindings,
  `Dns*`, `ExtraHosts` — Docker rejects these in `container:` mode) → `rm`
  **without `-v`** → `create` (same name, **same `Mounts` incl. anonymous
  volumes**) → `start`. Socket-only (adds `jq`/`curl` to the image). **No new
  proxy permission:** the shipped proxy already sets `POST=1` (for restart), and
  tecnativa has no granular create/delete flag, so create + start + rm all ride on
  `POST=1`. (A hardened `POST=0` + `ALLOW_RESTARTS` setup is the exception —
  recreate is denied there and the monitor falls back to alert.) Faithful-ish;
  the fidelity burden is ours.
- **C — delegate to compose:** read `com.docker.compose.*` labels →
  `docker compose up -d --force-recreate <svc>`. Most faithful, but needs the
  compose plugin in the image, the host compose file(s) bind-mounted in, and a
  much broader proxy surface. Heaviest coupling.

## Decision
- **Mechanism = B (Docker-API reconstruct).** It satisfies #20's request, stays
  socket-only (Tenet 3), and avoids coupling the monitor to the user's on-disk
  compose layout. **C is rejected** as too heavy for a watchdog.
- **Default = B, attempted automatically (default ON).** When a dependent is
  stranded loopback-only and its `NetworkMode` target ≠ the current gluetun ID,
  the monitor reconstructs it. **Capability-gated:** if the Docker connection
  denies create/rm (a hardened `POST=0` proxy) or the reconstruct fails, it
  **falls back to a loud, actionable alert** — never leaves a dependent down
  silently. **`AUTO_RECREATE=0`** disables auto-recreate for anyone who has the
  permission but prefers alert-only.
- **Non-destructive (validated) + no new proxy permission → a MINOR release with a
  prominent call-out.** The data-loss test confirms named + bind + anonymous
  volume data survive (copy `Mounts`, `rm` without `-v`); only the ephemeral
  writable layer is lost. The shipped `POST=1` already permits create/rm. The
  CHANGELOG/README **headline** that the monitor now auto-recreates stranded
  dependents by default (**new container ID, brief downtime**, volumes preserved,
  ephemeral layer discarded), with `AUTO_RECREATE=0` to opt out. It is a
  default-behavior change, mitigated by being non-destructive and loudly
  documented.
- B must: perform the conflicting-field stripping above, **preserve named volumes,
  binds, and anonymous volumes** (copy `Mounts`; `rm` without `-v`), and
  **verify** (running + non-`lo`) after acting; on failure, fall back to a loud
  alert rather than leaving the dependent down silently.

## Consequences
- The original #20 ask (auto-recreate) is delivered **by default**, with a
  loud-alert fallback when it can't act (or fails) and `AUTO_RECREATE=0` to
  disable. Least-privilege still holds — no new proxy permission is required.
- It adds `jq`/`curl` to the image but needs **no new proxy permission**: the
  shipped `POST=1` (already set for restart) covers create/start/rm, since
  tecnativa has no granular create/delete flag. Only a hardened `POST=0` +
  `ALLOW_RESTARTS` setup would deny it → graceful fallback to alert.
- **Volume data is preserved (validated empirically)** when the reconstruct
  copies all `Mounts` and `rm`s without `-v` (named, bind, *and* anonymous
  volumes). The only unavoidable loss is the ephemeral writable layer. Documented
  risks are the behavioral ones: new container ID, brief downtime, and
  reconstruction-fidelity drift.
- Fidelity burden: a representative dependent should be round-tripped in tests to
  catch dropped fields. Reconstruction drift is the main ongoing risk.
- Prevention still belongs in the docs regardless of this choice: exclude gluetun
  from Watchtower (or use a post-update hook), and/or compose
  `depends_on: { condition: service_healthy, restart: true }` for compose-driven
  recreates. The monitor is the safety net, not a license to yank the netns owner
  out from under its dependents.
