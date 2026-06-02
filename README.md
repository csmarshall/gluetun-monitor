# Gluetun Monitor

<p align="center">
  <img src="logo.svg" alt="Gluetun Monitor Logo" width="150" height="150">
</p>

<p align="center">
  <a href="https://github.com/csmarshall/gluetun-monitor/actions/workflows/ci.yml"><img src="https://github.com/csmarshall/gluetun-monitor/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/csmarshall/gluetun-monitor/releases"><img src="https://img.shields.io/github/v/release/csmarshall/gluetun-monitor" alt="GitHub release"></a>
  <a href="https://hub.docker.com/r/chasmarshall/gluetun-monitor"><img src="https://img.shields.io/docker/pulls/chasmarshall/gluetun-monitor" alt="Docker Pulls"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://buymeacoffee.com/cs_marshall"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?logo=buy-me-a-coffee&logoColor=black" alt="Buy Me a Coffee"></a>
</p>

A lightweight Docker container that monitors VPN connectivity through [Gluetun](https://github.com/qdm12/gluetun) and automatically recovers from connection failures — restarting Gluetun **and** healing dependent containers that get stranded when Gluetun's network namespace is rebuilt.

> **v2.0.0** is a Python reimplementation that adds **dependent-aware health**:
> the monitor now measures each dependent directly instead of inferring stack
> health from the gateway, and self-heals a dependent that is stranded
> loopback-only after a Gluetun restart/recreate (issue #20). Behavior and
> configuration are backward compatible; see the [CHANGELOG](CHANGELOG.md) and
> [ADR-0007](docs/adr/0007-reimplement-in-python.md). The design record lives in
> [`docs/`](docs/) (tenets + ADRs + the per-loop state machine).

## Links

- **GitHub Repository**: https://github.com/csmarshall/gluetun-monitor
- **Docker Hub**: https://hub.docker.com/r/chasmarshall/gluetun-monitor
- **GitHub Container Registry**: https://github.com/csmarshall/gluetun-monitor/pkgs/container/gluetun-monitor
- **Releases**: https://github.com/csmarshall/gluetun-monitor/releases

**Pull the image:**
```bash
# From Docker Hub
docker pull chasmarshall/gluetun-monitor:2

# From GitHub Container Registry
docker pull ghcr.io/csmarshall/gluetun-monitor:2
```

## Features

- **Multi-site health checking** - Tests connectivity to multiple endpoints simultaneously
- **Parallel testing** - All sites tested concurrently for fast detection (bounded by single timeout)
- **Dependent-aware health (#20)** - Measures each dependent directly (interface check + a per-container DNS/connectivity probe) instead of trusting the gateway; stops reporting healthy when a dependent is stranded
- **Self-healing dependents** - Restarts a dependent that shares Gluetun's current namespace; **non-destructively recreates** (volumes preserved) one stranded by a Gluetun *recreate* (new container id)
- **Auto-discovery** - Automatically finds containers using Gluetun's network, and remembers them across cycles so a dependent isn't lost when Gluetun's id changes
- **Automatic recovery** - Restarts Gluetun on connectivity failure and re-verifies before touching dependents
- **Endpoint logging** - Logs VPN server details (server, country, city, IP) on failures and recoveries
- **Configurable thresholds** - Consecutive failure counts, independently tunable for Gluetun and for dependents
- **Low resource usage** - Uses `wget --spider` (headers only, no body download), bounded dependent fan-out

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                       Gluetun Monitor (each loop)               │
├─────────────────────────────────────────────────────────────────┤
│  1. Test sites through Gluetun's network (the root signal)      │
│  2. If site failures exceed FAIL_THRESHOLD:                     │
│       restart Gluetun, wait healthy + DNS, re-verify the set    │
│       (only proceed if the tunnel is actually restored)         │
│  3. For each dependent (every loop — the #20 fix):              │
│       a. Interface check: stranded loopback-only?               │
│       b. Else probe one shuffled name (DNS + connectivity)      │
│       c. On confirmed failure, remediate:                       │
│            • shares Gluetun's current id → docker restart       │
│            • Gluetun recreated (id moved) → recreate (volumes    │
│              preserved); disabled/denied → report FAILED        │
│       d. Verify (running + non-loopback interface)              │
└─────────────────────────────────────────────────────────────────┘
```

The full per-loop state machine (22 nodes) is in
[ADR-0006](docs/adr/0006-per-dependent-viability-testing.md); the
restart-vs-recreate decision is [ADR-0004](docs/adr/0004-dependent-aware-health.md).

## Upgrading from v1 (v1 is end-of-life)

**v1 (the original bash implementation, image tag `:1`) is end-of-life.** v2 is
the supported line — please move to it. The upgrade is a **drop-in config
change**: v2 reads the **same env vars** (same names and defaults), the same
`/config/sites.conf` and `/logs` paths, and needs the **same socket-proxy
permissions** (`CONTAINERS` / `POST` / `EXEC`). In the common case you change
only the image tag and it keeps working — we validate that the v1 env surface
boots cleanly against a socket proxy as part of testing.

**Image tags** (full policy: [docs/VERSIONING.md](docs/VERSIONING.md)):
- **`:2`** — **recommended for production**: you get every v2.x patch and never a
  surprise future major.
- `:2.0.0` — fully pinned to one release (reproducible; you update deliberately).
- `:latest` — always the newest release; **will** eventually roll to a future v3
  major, so don't use it for unattended updates of a container-restarting watchdog.
- **`:1`** — frozen v1, kept only as a **rollback anchor**; unsupported (EOL).

**Two behavior changes to know about** (the config interface is compatible, but
v2 does more):
1. **It now heals dependents by default** (the #20 fix): a stranded dependent is
   restarted (same gluetun id) or **recreated** (id changed — volumes preserved;
   see [data safety](#what-it-will-and-wont-do-and-why-your-data-is-safe)). To
   stay close to v1's behavior, set `AUTO_RECREATE=0` (alert instead of recreate)
   and/or `DEPENDENT_VIABILITY=0` (interface/strand check only, no L7 probing).
2. **Config is validated; bad config is now fatal.** v2 refuses to start on a
   few things v1 tolerated silently — an empty `sites.conf`, a malformed env
   value, or an explicit `DEPENDENT_CONTAINERS` naming a container that doesn't
   exist. If startup fails after the upgrade, the error message says exactly what
   to fix (see [Configuration is validated](#configuration-is-validated--sane-defaults-but-bad-config-is-fatal)).

**Rollback** is one step: repin the image to `:1`.

## Quick Start

### 1. Pull the image

```bash
docker pull ghcr.io/csmarshall/gluetun-monitor:2
```

### 2. Copy example configs

```bash
# If cloning the repo:
cp docker-compose.yml.example docker-compose.yml
cp sites.conf.example sites.conf
```

### 3. Configure

Edit `docker-compose.yml`:
- Set `GLUETUN_CONTAINER` to your gluetun container name
- Adjust `TZ` for your timezone

Edit `sites.conf` with endpoints to test:

```conf
# Sites to test for VPN connectivity
https://www.google.com
https://cloudflare.com
https://1.1.1.1
# Add sites you need to reach through VPN
```

Alternatively (or in addition), you can provide sites entirely via the **`SITES`**
environment variable — handy if you'd rather not mount a file:

```yaml
environment:
  - SITES=https://www.google.com,https://cloudflare.com
```

The two sources are **unioned** (file + env, de-duplicated). At least one site is
required — see [Configuration](#configuration). The monitor must reach a real
endpoint set to do its job, so it refuses to start with none.

### 4. Deploy

```bash
docker compose up -d
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DOCKER_HOST` | *(unset)* | Docker daemon endpoint. Set to `tcp://docker-socket-proxy:2375` when using a socket proxy |
| `GLUETUN_CONTAINER` | `gluetun` | Name of the Gluetun container to monitor (must exist, else fatal) |
| `CONFIG_FILE` | `/config/sites.conf` | Path to the sites file (re-read each loop → live-editable) |
| `SITES` | *(unset)* | Comma-separated test URLs, **unioned** with the sites file. Set at startup (not live-reloaded) |
| `DEPENDENT_CONTAINERS` | `auto` | `auto` to discover dynamically, or comma-separated list (every named container must exist, else fatal) |
| `EXCLUDE_CONTAINERS` | *(unset)* | Comma-separated container names to **never** manage (denylist). Filters auto-discovery and subtracts from an explicit list; exclude wins on overlap |
| `CHECK_INTERVAL` | `30` | Seconds between health checks |
| `TIMEOUT` | `10` | Seconds to wait for each site test |
| `FAIL_THRESHOLD` | `2` | Consecutive site failures before restarting Gluetun |
| `HEALTHY_WAIT_TIMEOUT` | `120` | Max seconds to wait for Gluetun to become healthy after restart |
| `DEPENDENT_CONTAINER_FAILURES` | *(= `FAIL_THRESHOLD`)* | Consecutive per-dependent viability failures before remediating that dependent |
| `MAX_PARALLEL_CHECKS` | `6` | Cap on concurrent `docker exec` probes across dependents per loop |
| `DEPENDENT_VIABILITY` | `1` | Per-dependent L7 DNS/connectivity probe. `0` = interface/strand check only (no URL fetch); the interface check is always on |
| `MAX_JITTER_MS` | `0` | Optional per-dispatch jitter (ms) to spread the dependent probe burst. `0` = off (the concurrency cap already bounds it) |
| `DRY_RUN` | `0` | Observe-only: run all detection/probing but **take no action** — log `[DRY-RUN] would …` instead of restarting/recreating. For soak-testing alongside an active monitor |
| `STATS_FILE` | `/logs/site-stats.json` | Where persistent per-site stats are written (best-effort, atomic; survives restarts). See [Site stats & flaky-site advisory](#site-stats--flaky-site-advisory) |
| `STATS_RETENTION_DAYS` | `90` | Drop a site's stats if it hasn't been tested (e.g. removed from `sites.conf`) for this many days; `0` keeps them forever |
| `ADVISORY_WINDOW` | `86400` | Window (seconds) for the flaky-site advisory |
| `ADVISORY_MIN_RESTARTS` | `5` | Minimum gluetun restarts in the window before an advisory can fire |
| `ADVISORY_DOMINANCE` | `0.5` | Fraction of those restarts one site must cause to be flagged flaky |
| `AUTO_RECREATE` | `1` | Recreate a dependent stranded by a Gluetun recreate (id changed). Set `0` to disable → such a dependent is reported FAILED instead |
| `DNS_WAIT_TIMEOUT` | `30` | Max seconds to wait for Gluetun DNS to stabilize after a restart |
| `LOG_LEVEL` | `INFO` | `DEBUG` to include per-site/per-dependent detail lines |
| `TZ` | `UTC` | Timezone for log timestamps |

### Configuration is validated — sane defaults, but bad config is fatal

The design goal is **good results with zero extra config**: with a container named
`gluetun` and a site to test, everything else has a sane default. But to protect
the wider system, the monitor **refuses to start** (exits non-zero) rather than
*guess* around misconfiguration:

- **Malformed env value** (a non-integer `CHECK_INTERVAL`, an unrecognized
  `AUTO_RECREATE`, an invalid `LOG_LEVEL`, …) → fatal. An *unset* variable is just
  its default; a *set-but-unparseable* one is an error we won't paper over.
- **No testable sites** (no `sites.conf` *and* no `SITES`, or both empty) → fatal.
  A monitor that tests nothing would report fake-green forever.
- **`GLUETUN_CONTAINER` doesn't exist** → fatal (nothing to monitor).
- **Explicit `DEPENDENT_CONTAINERS` names a container that doesn't exist** → fatal.
  You told us exactly what to manage; we won't silently drop or guess around a
  name we can't find (it could mean acting on the wrong container). `auto` finding
  zero is fine — that's gluetun-only monitoring, a valid setup.

### Variable Details

#### Sites — `CONFIG_FILE` + `SITES`
Test URLs come from two **unioned** sources (de-duplicated):
- **`CONFIG_FILE`** (default `/config/sites.conf`) — one URL per line, `#` comments
  allowed. **Re-read every loop**, so adding/removing a URL takes effect on the
  next check with no restart.
- **`SITES`** — a comma-separated list in the environment, for config-via-env
  parity with the other knobs (no file mount needed). Fixed at process start —
  changing it requires a container restart.

Provide either, or both. At least one URL total is required or the monitor won't
start (see above).

#### `DOCKER_HOST`
When unset, the Docker CLI connects via the local socket (`/var/run/docker.sock`). Set this to `tcp://<proxy-host>:2375` to connect through a [Docker socket proxy](#docker-socket-proxy) instead of mounting the socket directly. See the [Docker Socket Proxy](#docker-socket-proxy) section for setup details.

#### `GLUETUN_CONTAINER`
The name of your Gluetun container as shown in `docker ps`. This is the container that will be:
- Used to execute site connectivity tests (via `docker exec`)
- Monitored for health status
- Restarted when connectivity fails
- Used to extract VPN endpoint information from logs

#### `DEPENDENT_CONTAINERS`
Controls which dependents are watched and healed:
- `auto` - Automatically discovers containers using `network_mode: "container:<GLUETUN_CONTAINER>"` (queries the Docker API for each running container's `NetworkMode`). Discovering zero is fine — gluetun-only monitoring.
- `container1,container2` - Comma-separated list of container names. **Every name must exist at startup or the monitor exits** — an explicit list is a contract, and we won't guess around a missing name. If your dependents start alongside the monitor, order startup (`depends_on:`) so they exist first, or use `auto`.

#### `EXCLUDE_CONTAINERS`
A comma-separated **denylist** of containers the monitor must **never** manage —
never interface-checked, viability-tested, restarted, or recreated. Most useful
with `auto` ("discover everything *except* these") so you keep auto-discovery of
new dependents while protecting specific ones. It also subtracts from an explicit
`DEPENDENT_CONTAINERS` list.

If a name appears in **both** `DEPENDENT_CONTAINERS` and `EXCLUDE_CONTAINERS`,
**exclude wins** and the monitor warns — the contradiction resolves toward *not*
touching the container ("first, do no harm") rather than failing. An exclude name
that matches no container warns too (likely a typo — the container you meant to
protect would otherwise still be managed).

#### `CHECK_INTERVAL`
Time in seconds between health check cycles.

**Note:** sites are tested concurrently, bounded by `MAX_PARALLEL_CHECKS` (default 6). With ≤6 sites a cycle's tests finish within one `TIMEOUT`; with more, they run in batches, so a cycle can take up to `ceil(sites / MAX_PARALLEL_CHECKS) × TIMEOUT`.

#### `TIMEOUT`
Maximum seconds to wait for each site to respond. Tests run concurrently (up to `MAX_PARALLEL_CHECKS` at a time), so this bounds each batch rather than each individual site.

Uses `wget --spider` which only fetches headers (no response body downloaded).

### Site Test Success/Failure Logic

The monitor distinguishes between **connectivity failures** (VPN broken) and **site errors** (VPN working, site returned an error):

| wget Exit Code | Meaning | Treated As | Rationale |
|----------------|---------|------------|-----------|
| 0 | Success (HTTP 2xx/3xx) | **PASS** | Site responded successfully |
| 6 | Authentication required | **PASS** | Site responded (VPN working) |
| 8 | Server error (HTTP 4xx/5xx) | **PASS** | Site responded (VPN working) |
| 4 | Network failure | **FAIL** | DNS or connection failed |
| 5 | SSL verification failure | **FAIL** | Possible MITM or connectivity issue |
| 1-3, 7 | Other errors | **FAIL** | Various connectivity issues |

**Key insight:** If a site returns HTTP 403 Forbidden or 503 Service Unavailable, the VPN is working - the site just doesn't like the request. Only actual network/DNS failures indicate a VPN problem.

#### `FAIL_THRESHOLD`
Number of **consecutive** failures for a site before triggering a restart. This prevents restarts from transient network blips.

Example with `FAIL_THRESHOLD=2`:
- Check 1: Site fails → Counter: 1 (no action)
- Check 2: Site fails → Counter: 2 (triggers restart)
- After restart: Counter reset to 0

#### `HEALTHY_WAIT_TIMEOUT`
Maximum seconds to wait for Gluetun to report "healthy" status after a restart. Gluetun must have a healthcheck configured for this to work.

If Gluetun doesn't become healthy within this timeout, the monitor logs an error but continues operating.

### Dependent Container Discovery

By default (`DEPENDENT_CONTAINERS=auto`), the monitor automatically finds all containers that depend on Gluetun by querying the Docker API for containers with:

```
network_mode: "container:<GLUETUN_CONTAINER>"
```

This just works out of the box - no configuration needed. Discovery runs at startup (for logging) and again before each restart operation to ensure newly added containers are included.

**Note:** Containers added after startup will be discovered and restarted when the next failure triggers a recovery. There's no continuous polling for new containers during normal operation.

#### Advanced: Manual Override

In rare cases where you need explicit control (e.g., restart only specific containers, or include containers that don't use network_mode), you can specify a manual list:

```yaml
environment:
  - DEPENDENT_CONTAINERS=container1,container2,container3
```

## Dependent-Aware Health (issue #20)

Dependents that use `network_mode: "service:gluetun"` (or `container:gluetun`)
share Gluetun's **network namespace**, and that share is bound to a specific
container **instance**. When Gluetun is restarted or recreated (e.g. a Watchtower
image update), those dependents are left **stranded loopback-only** — `Running`,
but with only the `lo` interface and no route to the internet. Because v1.x
tested connectivity *only from inside Gluetun*, every check passed and the
monitor reported healthy while the stack was broken.

v2 closes that blind spot by measuring each dependent directly, every loop:

1. **Interface check** — `ls /sys/class/net` inside the dependent. Only `lo` ⇒
   stranded (a hard failure; re-checked once because it won't self-heal).
2. **Viability probe** — for a live dependent, one **shuffled** resolvable name
   from your `sites.conf` is fetched *from inside that container*, proving its own
   DNS + connectivity. A different name each loop means
   `DEPENDENT_CONTAINER_FAILURES` consecutive failures = "this container can't
   reach N *different* names" (a container fault), not "one URL was down". If
   `sites.conf` has only IP literals, the monitor warns that dependent DNS can't
   be validated and falls back to a connectivity-only IP probe.
3. **Remediation** — keyed on the dependent's `NetworkMode` target vs Gluetun's
   current container id:
   - **same id** (incl. the monitor's own Gluetun restart) → `docker restart` the
     dependent — it rejoins the rebuilt namespace. Cheap, no new permissions.
   - **id changed** (Gluetun was replaced) → **recreate** the dependent.
     `NetworkMode` is immutable, so there is no in-place fix. The recreate is
     **non-destructive**: all mounts — named, bind, and **anonymous** volumes —
     are carried forward by source and the old container is removed *without*
     `-v`, so only the ephemeral writable layer is lost.
   - recreate **disabled** (`AUTO_RECREATE=0`) or denied → the dependent is
     reported **FAILED** loudly rather than papered over.

A dependent that fails counts up per-container and resets on any passing loop —
counters are in-memory with no backoff (recovery is cheap and non-destructive, so
the monitor simply re-acts each cycle). distroless/scratch dependents with no
shell can't be exec-probed and fall back to the inspect-based signals.

> **Note on discovery + recreate:** a dependent stranded by a Gluetun *recreate*
> still points at the dead old id, so auto-discovery (which matches Gluetun's
> *current* id/name) no longer recognizes it. A running monitor remembers
> dependents across cycles and handles this automatically. If you start the
> monitor *after* such a strand already exists, name the dependents explicitly
> via `DEPENDENT_CONTAINERS` so they're tracked from the first loop. At startup
> the monitor logs a **WARN** naming any running container stranded on a
> now-dead netns parent, so you know which ones to add — it won't auto-recreate
> them, since an orphan whose parent is gone can't be confirmed as *this*
> gluetun's dependent (first, do no harm).

## What it will and won't do (and why your data is safe)

gluetun-monitor is a watchdog that *restarts and recreates containers*, so it
owes you a clear contract about exactly what it touches — and what it can't.

### It WILL
- Restart **gluetun** on a confirmed connectivity failure (to force a new endpoint).
- Restart a **dependent** that shares gluetun's *current* network namespace.
- **Recreate** a dependent stranded by a gluetun *recreate* (its netns id moved) —
  non-destructively (see below). On by default; `AUTO_RECREATE=0` disables it.
- Report a loud, explicit **FAILED** state when it can't heal — never fake-green.

### It WON'T
- Pull or build images, or change image tags/versions.
- Edit your compose files, env, or container configuration.
- **Delete volumes or data** (it never runs `docker rm -v`).
- Touch containers that aren't gluetun dependents, or any you list in
  `EXCLUDE_CONTAINERS`.
- Act on targets you didn't choose — sites you didn't configure, or an orphan it
  can't confidently attribute to gluetun (Tenet 1).
- Start at all on malformed config — it fails loud instead of guessing.

### Why your data is safe across a recreate
A "recreate" replaces the container **object**, not its data. Docker volumes are
owned by the daemon, not the container, so:

- **Named volumes, bind mounts, and even anonymous volumes are carried forward** —
  the monitor reads the old container's mounts and re-attaches the *same* volumes
  by source on the new container, then removes the old container **without `-v`**.
  No volume is ever deleted. (Mechanism + empirical data-loss test: [ADR-0005](docs/adr/0005-recreate-mechanism.md).)
- The **only** thing lost is the container's ephemeral **writable layer** — files
  written *inside* the container that aren't on a volume. That layer is ephemeral
  by design (recreated from the image). Anything you care about lives on a volume,
  and volumes survive.

### What's actually at risk vs. what only sounds risky

| Sounds alarming | The reality |
|---|---|
| "It *recreates* my container!" | New container **ID** and a **brief downtime** during the swap. Volume data is preserved. |
| "A watchdog with Docker access" | Behind the default socket-proxy it's limited to container ops (list/inspect/logs/restart/exec/create/remove) — no image pull/build, no host access. |
| "Reconstruction might drop a setting" | The genuine residual risk: a recreate copies the container's config + mounts, and a fidelity bug could drop a *non-volume* setting. Mitigations: it **verifies** after recreating, `AUTO_RECREATE=0` disables recreate entirely, and `EXCLUDE_CONTAINERS` protects specific containers. |

### Your controls
- **`AUTO_RECREATE=0`** — never recreate; alert instead (restart-only recovery).
- **`EXCLUDE_CONTAINERS=...`** — a denylist of containers to never touch.
- **Socket proxy** (default) — cap the Docker API surface the monitor can use.

## Site stats & flaky-site advisory

A single flaky **test site** (one that intermittently times out or SSL-errors)
can trip `FAIL_THRESHOLD` and trigger a gluetun restart — even though the tunnel
is fine (your other sites pass). Restarting *can* fix a genuinely blocked
endpoint, so the monitor still tries it; but to stop you chasing the wrong thing,
it keeps a **persistent, rear-looking record** of how each site behaves and
**tells you** which one is the troublemaker.

It writes a human-readable JSON sidecar (`STATS_FILE`, default
`/logs/site-stats.json`) with, per site: total polls, total failures (→ failure
rate), failure **episodes** and the average episode length in polls (how long it
typically stays down when it breaks), the **longest** such streak, how many
gluetun restarts it triggered, and first-seen / last-good / last-failure
timestamps. It's written **every loop, crash- and power-loss-safely** (temp file
+ fsync + atomic rename), survives monitor restarts, and is best-effort (a
missing/unwritable/corrupt file never blocks the monitor). A site removed from
`sites.conf` is kept for `STATS_RETENTION_DAYS` (default 90) then pruned.

When one site dominates the recent restarts, the monitor logs a **flaky-site
advisory** (once, not per loop):

```
[WARN] FLAKY SITE: https://dognzb.cr caused 17 of the last 22 gluetun restarts
over the last 24h — it may be flaky; consider reviewing or removing it from sites.conf
```

That's the signal to prune that site from `sites.conf` (which is re-read live, so
no restart needed). Tune with `ADVISORY_WINDOW`, `ADVISORY_MIN_RESTARTS`, and
`ADVISORY_DOMINANCE`. See [ADR-0008](docs/adr/0008-persistent-site-stats-and-advisory.md).

> **By design, the monitor does not auto-suppress a flaky site** — it keeps
> applying the cheap restart fix and escalates to you. (A future automatic
> back-off is possible; it would be opt-in.)

## Docker Compose Example

### Minimal Configuration (with socket proxy)

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: gluetun-monitor-socket-proxy
    restart: unless-stopped
    environment:
      - CONTAINERS=1
      - POST=1
      - EXEC=1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker-proxy

  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:2
    # Or from Docker Hub: chasmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun  # Name of your Gluetun container
    volumes:
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

The monitor will automatically discover dependent containers and use sensible defaults. The socket proxy restricts Docker API access to only the endpoints gluetun-monitor needs.

### Full Configuration (all options)

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: gluetun-monitor-socket-proxy
    restart: unless-stopped
    environment:
      - CONTAINERS=1
      - POST=1
      - EXEC=1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker-proxy

  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:2
    # Or from Docker Hub: chasmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - TZ=UTC
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun
      - DEPENDENT_CONTAINERS=auto      # auto-discovery (default)
      - CHECK_INTERVAL=30              # seconds between checks
      - TIMEOUT=10                     # seconds per site test
      - FAIL_THRESHOLD=2               # consecutive site failures to restart gluetun
      - HEALTHY_WAIT_TIMEOUT=120       # seconds to wait for healthy status
      # --- v2 dependent-aware knobs (all optional) ---
      - DEPENDENT_CONTAINER_FAILURES=2 # consecutive per-dependent failures to remediate (default = FAIL_THRESHOLD)
      - MAX_PARALLEL_CHECKS=6          # cap on concurrent dependent probes
      - AUTO_RECREATE=1                # recreate a dependent stranded by a gluetun recreate (0 to disable)
      - DNS_WAIT_TIMEOUT=30            # seconds to wait for gluetun DNS after restart
      - LOG_LEVEL=INFO                 # DEBUG for per-site/per-dependent detail
    volumes:
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

### Alternative: Direct Socket Mount

If you prefer a simpler setup without the socket proxy, you can mount the Docker socket directly:

```yaml
services:
  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    network_mode: none
    environment:
      - GLUETUN_CONTAINER=gluetun
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
```

Note: This gives the container full read access to the Docker API. The socket proxy approach above is recommended for production use.

## Log Output

### Startup
```
[2025-01-15 10:00:00] [INFO] Gluetun Monitor starting...
[2025-01-15 10:00:00] [INFO] Config: CHECK_INTERVAL=30s, TIMEOUT=10s, FAIL_THRESHOLD=2, DEPENDENT_CONTAINER_FAILURES=2, AUTO_RECREATE=1
[2025-01-15 10:00:00] [INFO] Monitoring container: gluetun
[2025-01-15 10:00:00] [INFO] Prerequisites check passed
[2025-01-15 10:00:00] [INFO] Docker connection: socket proxy (tcp://docker-socket-proxy:2375)
[2025-01-15 10:00:00] [INFO] Dependent containers (auto-discovery): app1,app2,app3
[2025-01-15 10:00:00] [ENDPOINT] Status: STARTUP | IP: 203.x.x.x | Country: United States | City: New York | VPN Server: us123.vpn.com | Reason: Monitor starting
```

### Gluetun connectivity failure + recovery
```
[2025-01-15 10:10:00] [WARN] Site https://example.com failed 2 consecutive times - THRESHOLD REACHED - Network failure (DNS or connection)
[2025-01-15 10:10:00] [ERROR] Failed sites (exceeded threshold): https://example.com
[2025-01-15 10:10:00] [WARN] Health check failed, initiating recovery...
[2025-01-15 10:10:00] [ENDPOINT] Status: FAILING | IP: 203.x.x.x | Country: United States | City: New York | VPN Server: us123.vpn.com | Reason: Site connectivity test failed
[2025-01-15 10:10:05] [INFO] Restarting gluetun to force new endpoint...
[2025-01-15 10:10:35] [INFO] gluetun is healthy after 30s
[2025-01-15 10:10:38] [INFO] DNS and connectivity verified after 3s
[2025-01-15 10:10:40] [ENDPOINT] Status: NEW | IP: 89.x.x.x | Country: Germany | City: Frankfurt | VPN Server: de456.vpn.com | Reason: After restart
[2025-01-15 10:10:40] [INFO] Connectivity verified after restart
```

### Dependent stranded by a Gluetun recreate (self-healed)
```
[2025-01-15 11:00:00] [WARN] Remediating dependent qbittorrent: stranded loopback-only
[2025-01-15 11:00:00] [WARN] qbittorrent netns target moved (gluetun recreated) — recreate required
[2025-01-15 11:00:00] [WARN] Recreating qbittorrent (re-homing netns onto gluetun 9f3c1a2b4d5e)
[2025-01-15 11:00:02] [INFO] qbittorrent recreated as 7a1b2c3d4e5f and started
[2025-01-15 11:00:04] [INFO] qbittorrent verified healthy after remediation
```

## Requirements

- Docker with API access (via [socket proxy](#docker-socket-proxy) or direct socket mount)
- Gluetun container with a healthcheck configured
- Dependent containers using `network_mode: "container:<gluetun>"`

## How Gluetun Network Mode Works

Containers can share Gluetun's network stack using Docker's container network mode:

```yaml
# Gluetun container
services:
  gluetun:
    image: qmcgaw/gluetun
    container_name: gluetun
    ports:
      - 8080:8080  # Expose ports for dependent apps here
    # ... VPN configuration

  # App using Gluetun's network
  myapp:
    image: myapp:latest
    network_mode: "container:gluetun"
    depends_on:
      - gluetun
    # Note: ports must be defined on gluetun, not here
```

When Gluetun restarts, containers using its network lose connectivity and typically need to be restarted. This monitor automates that process.

## How Auto-Discovery Works

The monitor automatically discovers dependent containers by communicating with the Docker daemon — either through a [socket proxy](#docker-socket-proxy) (recommended) or a direct [Docker socket](https://docs.docker.com/engine/reference/commandline/dockerd/#daemon-socket-option) mount.

### Docker API Access

The monitor needs access to the Docker API to list, inspect, restart, and exec into containers. There are two ways to provide this:

**Socket proxy (recommended):** Set `DOCKER_HOST=tcp://socket-proxy:2375` and connect through an isolated network. The proxy limits API access to only the endpoints needed. See [Docker Socket Proxy](#docker-socket-proxy).

**Direct socket mount:** Mount `/var/run/docker.sock` into the container. Simpler but grants full Docker API access.

```yaml
# Socket proxy (recommended)
environment:
  - DOCKER_HOST=tcp://docker-socket-proxy:2375

# Or direct mount (simpler, less secure)
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

This is the same mechanism used by tools like [Portainer](https://www.portainer.io/), [Traefik](https://traefik.io/), and [Watchtower](https://containrrr.dev/watchtower/).

### Discovery Process

When discovery runs, the monitor:

1. **Queries Docker** for all running container IDs via `docker ps -q`
2. **Inspects each container** to get its `NetworkMode` setting via `docker inspect`
3. **Matches containers** where `NetworkMode` equals `container:<gluetun-name>` or `container:<gluetun-id>`
4. **Returns the list** of dependent container names

```bash
# What the monitor does internally:
docker inspect --format='{{.HostConfig.NetworkMode}}' <container_id>
# Returns: "container:gluetun" or "container:abc123def456..."
```

### When Discovery Runs

- **At startup** - For logging which containers will be managed
- **Every loop** - So newly added containers are picked up promptly, and so a
  dependent is re-checked each cycle. Discovered containers are also remembered
  across cycles, so a dependent isn't lost when Gluetun's container id changes
  (see [Dependent-Aware Health](#dependent-aware-health-issue-20)).

This approach means no configuration is needed - containers are discovered dynamically based on their actual Docker configuration.

For more details on the Docker Engine API, see the [official documentation](https://docs.docker.com/engine/api/).

## Docker Socket Proxy

The recommended deployment uses a [Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy) to restrict which Docker API endpoints gluetun-monitor can access. Instead of mounting the Docker socket directly (which grants full API access), the proxy exposes only the specific capabilities needed:

| Proxy Setting | Required For |
|---------------|-------------|
| `CONTAINERS=1` | Listing, inspecting, and reading logs of containers; creating the replacement when recreating a stranded dependent |
| `POST=1` | Restarting, removing, and starting containers (the recreate path rides on the same `POST` flag — no new permission) |
| `EXEC=1` | Running connectivity/interface probes inside Gluetun and the dependents |

The proxy and gluetun-monitor communicate over an isolated Docker network (`docker-proxy`). Only the proxy container has access to the Docker socket.

## Security Considerations

- **Socket proxy (recommended):** Limits Docker API access to container operations (list, inspect, logs, restart, stop, exec). The monitor cannot pull images, build images, access volumes, manage networks, or perform other Docker operations. Note that `POST=1` enables all POST actions on allowed endpoints, not just restart.
- **Direct socket mount:** The Docker socket is mounted read-only (`:ro`), but socket operations (restart, exec) still function. The monitor can interact with any container — ensure your Docker environment is trusted.
- No credentials or sensitive data are logged
- Site test responses are discarded (headers only, no body)

## Building

```bash
docker compose build
```

Or manually:

```bash
docker build -t gluetun-monitor .
```

## Development

v2 is a Python package (`gluetun_monitor`). The whole monitor is unit-testable
without a Docker daemon — a fake client is injected at the Docker seam.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

ruff check gluetun_monitor tests     # lint
mypy gluetun_monitor                 # types (strict)
pytest                               # tests (incl. the differential suite vs. bash)
pytest --cov=gluetun_monitor         # with coverage
```

The legacy `gluetun-monitor.sh` is retained as the **differential oracle**: the
`differential`-marked tests execute its actual functions and assert the Python
port matches (the no-regressions gate from
[ADR-0007](docs/adr/0007-reimplement-in-python.md)). See
[DEVELOPMENT.md](DEVELOPMENT.md) for more.

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions welcome! Please open an issue or pull request.

## Acknowledgments

- [Gluetun](https://github.com/qdm12/gluetun) - The excellent VPN client container this monitor is designed for
