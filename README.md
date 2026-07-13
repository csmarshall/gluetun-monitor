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

```mermaid
flowchart TD
  L(["each loop"]) --> G["test sites through gluetun (the root signal)"]
  G --> GT{"a critical site failing<br/>FAIL_THRESHOLD loops in a row?"}
  GT -- "yes" --> GR["restart gluetun,<br/>wait healthy + DNS"]
  GT -- "no (or still counting)" --> D["check each dependent:<br/>interface + its own DNS/connectivity"]
  GR --> RV{"re-verify: are the sites back?"}
  RV -- "no / couldn't probe" --> STOP["alert, hold, and STOP —<br/>dependents left untouched,<br/>counters reset: it must re-earn<br/>the next restart"]
  RV -- "yes" --> D
  D --> DT{"can we tell?"}
  DT -- "stranded or unreachable" --> R["heal it — docker restart, or recreate<br/>(volumes preserved) if gluetun's id moved"]
  DT -- "no — crash-looping<br/>or no shell" --> U["report it: not counted healthy,<br/>never remediated"]
  DT -- "healthy" --> OK["healthy"]
  R --> V["verify: running + non-loopback interface"]
  STOP --> SLEEP["sleep CHECK_INTERVAL"]
  V --> SLEEP
  U --> SLEEP
  OK --> SLEEP
  SLEEP --> L
```

**Nothing is restarted on one bad probe.** A critical site must fail `FAIL_THRESHOLD` *consecutive* loops before it counts as a breach, and an advisory site never gates a restart at all. If a restart doesn't fix it, the monitor does not keep hammering: it raises an alert, leaves the dependents alone (it can't tell a stranded dependent from one that simply has no route yet), and clears its counters — so the next restart has to be earned from scratch.

The full per-loop state machine (22 nodes) is in
[ADR-0006](docs/adr/0006-per-dependent-viability-testing.md); the
restart-vs-recreate decision is [ADR-0004](docs/adr/0004-dependent-aware-health.md).

The monitor is deliberately **incurious** — it never learns what you route through the tunnel or what your containers are for, only that they have a shape it can measure ([ADR-0017](docs/adr/0017-incurious-monitor.md)). A dependent is *defined* as any container sharing Gluetun's network namespace; nothing else about it matters. What each capability your container ships unlocks — and what a `FROM scratch` container still gets — is written down in **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)**. Nothing there is a requirement to be met; it's an interface, and the monitor tells you which layer it's operating at rather than pretending.

## Upgrading from v1 (v1 is end-of-life)

**v1 (the original bash implementation, image tag `:1`) is end-of-life.** v2 is
the supported line — please move to it. The upgrade is a **drop-in config
change**: v2 reads the **same env vars** (same names and defaults), the same
`/config/sites.conf` and `/logs` paths, and needs the **same socket-proxy
permissions** (`CONTAINERS` / `POST` / `EXEC`). In the common case you change
only the image tag and it keeps working — we validate that the v1 env surface
boots cleanly against a socket proxy as part of testing.

> **⚠️ v1 has a known defect — it does not actually recover.** A later review of
> the bash script found a `set -e` bug (#81): the backgrounded site-test subshell
> exits at the failing `wget` **before** writing its result, for any non-zero wget
> exit. The upshot is that on a **real** connectivity failure, v1's
> `restart_gluetun` **never fires** — it logged a DEBUG "test result missing" line
> and did nothing (and sites answering 4xx/5xx were silently miscounted). So `:1`
> isn't just unsupported; its core self-healing was broken. **The Python v2 port
> fixes this** (and is what these docs describe). The `:1` image is retained only
> as a rollback anchor / differential-test oracle — do not run it in production.

**Image tags** (full policy: [docs/VERSIONING.md](docs/VERSIONING.md)):
- **`:2`** — **recommended for production**: you get every v2.x patch and never a
  surprise future major.
- `:2.0.0` — fully pinned to one release (reproducible; you update deliberately).
- `:latest` — always the newest **stable** release (excludes pre-releases, and an
  EOL v1 patch can't drag it back); **will** eventually roll to a future v3 major,
  so don't use it for unattended updates of a container-restarting watchdog.
- **`:1`** — frozen v1, kept only as a **rollback anchor**; unsupported (EOL).

**Two behavior changes to know about** (the config interface is compatible, but
v2 does more):
1. **It now heals dependents by default** (the #20 fix): a stranded dependent is
   restarted (same gluetun id) or **recreated** (id changed — volumes preserved;
   see [data safety](#what-it-will-and-wont-do-and-why-your-data-is-safe)). To
   stay close to v1's behavior, set `AUTO_RECREATE=0` (log a loud alert line
   instead of recreating) and/or `DEPENDENT_VIABILITY=0` (interface/strand check
   only, no L7 probing).
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
mkdir -p config && cp sites.conf.example config/sites.conf
```

`sites.conf` lives in a `./config/` directory that the compose file mounts as a
**directory** (`./config:/config:ro`), not as a single file — see
[Editing sites.conf live](#editing-sitesconf-live) for why this matters.

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
| `PUID` / `PGID` | *(unset → root)* | Run non-root (recommended): the entrypoint chowns `/logs` and drops to this uid/gid (LSIO-style). Unset = runs as root (drop-in). See [Running as non-root](#running-as-non-root-recommended) |
| [`DOCKER_HOST`](#docker_host) | *(unset)* | Docker daemon endpoint. Set to `tcp://docker-socket-proxy:2375` when using a socket proxy |
| [`GLUETUN_CONTAINER`](#gluetun_container) | `gluetun` | Name of the Gluetun container to monitor (must exist, else fatal) |
| [`CONFIG_FILE`](#sites--config_file--sites) | `/config/sites.conf` | Path to the sites file (re-read each loop → live-editable) |
| [`SITES`](#sites--config_file--sites) | *(unset)* | Comma-separated test URLs, **unioned** with the sites file. Set at startup (not live-reloaded) |
| [`DEPENDENT_CONTAINERS`](#dependent_containers) | `auto` | `auto` to discover dynamically, or comma-separated list (every named container must exist, else fatal) |
| [`EXCLUDE_CONTAINERS`](#exclude_containers) | *(unset)* | Comma-separated container names to **never** manage (denylist). Filters auto-discovery and subtracts from an explicit list; exclude wins on overlap |
| [`CHECK_INTERVAL`](#check_interval) | `30` | Seconds between health checks |
| [`TIMEOUT`](#timeout) | `10` | Per-request network timeout, applied identically to every probe — `wget --timeout` (gluetun site tests **and** the dependent-container probes) and `ping -W`. Overridable per URL — see [Per-URL tunables](#per-url-tunables) |
| `WGET_TRIES` | `1` | Attempts per `wget` probe (the shuffle + consecutive-failure thresholds, not retries, are how noise is tolerated). Overridable per URL — see [Per-URL tunables](#per-url-tunables) |
| [`FAIL_THRESHOLD`](#fail_threshold) | `2` | Consecutive site failures before restarting Gluetun |
| [`HEALTHY_WAIT_TIMEOUT`](#healthy_wait_timeout) | `120` | Max seconds to wait for Gluetun to become healthy after restart |
| `DEPENDENT_CONTAINER_FAILURES` | *(= `FAIL_THRESHOLD`)* | Consecutive per-dependent viability failures before remediating that dependent |
| `MAX_PARALLEL_CHECKS` | `6` | Cap on concurrent `docker exec` probes across dependents per loop |
| `DEPENDENT_VIABILITY` | `1` | Per-dependent L7 DNS/connectivity probe. `0` = interface/strand check only (no URL fetch); the interface check is always on |
| `DEPENDENT_VIABILITY_SAMPLES` | `1` | Sites each dependent tests per loop: `1` (one shuffled), `N` (N distinct), or `-1` (all). >1 is largely redundant (dependents share gluetun's netns), at `N` execs/dependent/loop |
| `MAX_JITTER_MS` | `0` | Optional per-dispatch jitter (ms) to spread the dependent probe burst. `0` = off (the concurrency cap already bounds it) |
| `DRY_RUN` | `0` | Observe-only: run all detection/probing but **take no action** — log `[DRY-RUN] would …` instead of restarting/recreating. For soak-testing alongside an active monitor |
| `STATS_FILE` | `/logs/site-stats.json` | Where persistent per-site stats are written (best-effort, atomic; survives restarts). See [Site stats & flaky-site advisory](#site-stats--flaky-site-advisory) |
| `STATS_RETENTION_DAYS` | `90` | Drop a site's stats if it hasn't been tested (e.g. removed from `sites.conf`) for this many days; `0` keeps them forever |
| `MONITOR_STATE_FILE` | `/logs/monitor-state.json` | Durable dependent memory: gluetun's container-id history + known dependent names, so dependents stranded by a gluetun recreate stay visible (and healable) across monitor restarts. Best-effort, atomic |
| `ADVISORY_WINDOW` | `86400` | Window (seconds) for the flaky-site advisory — and the recency window for `--suggest-tunables` (a site with no failure inside it yields no advice) |
| `ADVISORY_MIN_RESTARTS` | `5` | Minimum gluetun restarts in the window before an advisory can fire |
| `ADVISORY_DOMINANCE` | `0.5` | Fraction of those restarts one site must cause to be flagged flaky |
| `DEPENDENT_ADVISORY_WINDOW` | `86400` | Window (seconds) for the dependent-flapping advisory |
| `DEPENDENT_ADVISORY_MIN_REMEDIATIONS` | `5` | Remediations of one dependent in the window before it's flagged as flapping |
| `AUTO_RECREATE` | `1` | Recreate a dependent stranded by a Gluetun recreate (id changed). Set `0` to disable → such a dependent is reported FAILED instead |
| `DNS_WAIT_TIMEOUT` | `30` | Max seconds to wait for Gluetun DNS to stabilize after a restart |
| `LOG_LEVEL` | `INFO` | `DEBUG` to include per-site/per-dependent detail lines |
| `LOG_MAX_BYTES` | `10485760` | Rotate the `/logs` file at this size (≈10 MB); `0` disables rotation |
| `LOG_BACKUP_COUNT` | `5` | How many rotated log backups to keep (≈60 MB total at defaults) |
| `LOG_FILE` | `/logs/gluetun-monitor.log` | Path to the log file inside the `/logs` mount. Logs always go to stdout (`docker logs`) too; if this path isn't writable the monitor degrades to stdout-only rather than failing |
| `TZ` | `UTC` | Timezone for log timestamps |
| `APPRISE_URLS` | *(unset → off)* | Comma-separated [Apprise](https://github.com/caronc/apprise) URLs to push events to (ntfy/Discord/Telegram/email/webhook/…). Unset = notifications disabled. See [Notifications](#notifications) |
| `NOTIFY_LEVEL` | `attention` | Cumulative scope dial: `attention` (only when you must act) → `recovery` (self-healed incidents) → `activity` (non-fault changes) → `all` (firehose). See [Notifications](#notifications) |
| `NOTIFY_REPEAT_INTERVAL` | `0` | Re-notify cadence for an *ongoing* problem, in **loops**. `0` = announce once, then silent until it resolves; `N` = remind every `N` loops. Alerts are edge-triggered. See [Notifications](#notifications) |
| `NOTIFY_STATE_FILE` | `/logs/notify-state.json` | Where the active-alert lifecycle persists (so a monitor restart doesn't re-spam or miss a resolve). Best-effort. See [Notifications](#notifications) |
| `NOTIFY_TIMEOUT` | `10` | Max seconds to wait for a notification send before carrying on (sends run off the loop, so a slow backend can't stall the watchdog). See [Notifications](#notifications) |
| `WEDGE_ESCALATE_AFTER` | `3` | Consecutive **identical** remediation failures on one dependent before it's declared wedged: a distinct `dependent WEDGED` alert (with the operator runbook when the blocker is an unremovable parked twin) replaces the generic one, and remediation attempts back off. See [Notifications](#notifications) |
| `WEDGE_BACKOFF_CAP` | `600` | Ceiling (seconds) for the doubling remediation-retry backoff once wedged. Probes still run every loop — only the doomed remediation attempt is throttled. `0` = no backoff (retry every loop) but still escalate the alert |

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
  next check — the change is even logged by name (`Sites changed: added …`).
- **`SITES`** — a comma-separated list in the environment, for config-via-env
  parity with the other knobs (no file mount needed). Fixed at process start —
  changing it requires a container restart.

Provide either, or both. At least one URL total is required or the monitor won't
start (see above).

> **"Re-read every loop" needs a directory mount.** The compose file mounts
> `./config:/config:ro` (a directory) rather than the single `sites.conf` file so
> that a live edit reloads with **any** editor. A single-file bind mount pins the
> file's inode and misses atomic-save edits — see
> [Editing `sites.conf` live](#editing-sitesconf-live) for the full explanation.

Entries are sanity-checked: a URL with no host, or one that looks like a
command-line flag (leading `-`, which could otherwise be parsed as an option by
the in-container `wget`/`ping`), is **dropped with a startup warning** rather than
probed. The probes also pass URLs/hosts after a `--` end-of-options separator, so
a stray value can never be interpreted as a flag.

An entry may also carry a `|key=value` suffix (e.g. `https://slow.example|timeout=25`
or `https://geo-blocked.example|role=advisory`) to override the probe timeout/retries
or the site's **role** for **that URL only** — see
[Per-URL tunables](#per-url-tunables). A bare URL behaves exactly as before.

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

### Timeouts & retries — one model, everywhere

**Every timeout the monitor exposes, at a glance:**

| Knob | Default | Scope | Bounds | Per-URL? |
|---|---|---|---|---|
| `TIMEOUT` | `10` | per-request | each `wget --timeout` / `ping -W` (gluetun site tests **and** dependent probes) | ✅ [yes](#per-url-tunables) |
| `WGET_TRIES` | `1` | per-request | attempts per `wget` probe | ✅ [yes](#per-url-tunables) |
| `CHECK_INTERVAL` | `30` | loop cadence | sleep between check cycles | — |
| `HEALTHY_WAIT_TIMEOUT` | `120` | post-restart budget | wait for gluetun's healthcheck after a restart | — |
| `DNS_WAIT_TIMEOUT` | `30` | post-restart budget | poll for DNS to stabilize after a restart | — |
| `NOTIFY_TIMEOUT` | `10` | per-send | max wait for one (off-thread) notification send | — |
| `NOTIFY_REPEAT_INTERVAL` | `0` | alert cadence | reminder cadence for an ongoing problem (`0` = announce once) | — |
| `WEDGE_BACKOFF_CAP` | `600` | retry backoff | ceiling for the doubling remediation-retry delay on a wedged dependent (`0` = no backoff) | — |

(The Docker API client timeout isn't a separate knob — it's derived as
`max(TIMEOUT × 2, 60)`.)

**Only the two per-request knobs (`TIMEOUT`, `WGET_TRIES`) are per-URL-overridable**
([Per-URL tunables](#per-url-tunables)) — because they're the only ones that act on
*a specific URL's* probe. The rest are loop cadence (`CHECK_INTERVAL`), notification
plumbing, or **gluetun-restart-scoped wait budgets** (`HEALTHY_WAIT_TIMEOUT`,
`DNS_WAIT_TIMEOUT`) that wait on the *gluetun container* recovering, not on probing
any one site — so a per-URL value would be meaningless. See below for that split.

There is **one per-request timeout knob, `TIMEOUT`**, and it is applied identically
to every network probe the monitor makes:

| where | command | uses |
|---|---|---|
| gluetun site tests (through the tunnel) | `wget --spider --timeout=$TIMEOUT --tries=$WGET_TRIES` | `TIMEOUT`, `WGET_TRIES` |
| dependent viability (inside each dependent) | the same `wget …` | `TIMEOUT`, `WGET_TRIES` |
| DNS fallback (`ping`) | `ping -W $TIMEOUT` | `TIMEOUT` |
| post-restart DNS-readiness probe | the same `wget …` | `TIMEOUT`, `WGET_TRIES` |

So setting `TIMEOUT=10` **does** flow straight down to the `wget` run inside the
dependent containers (busybox or GNU — both honor `--timeout`). `getent`/`nslookup`
have no timeout flag and fall back to the container's resolver config (the cascade
moves on if a tool is slow/absent).

Don't confuse the **per-request** `TIMEOUT` with the **overall wait budgets** used
only after a gluetun restart: `HEALTHY_WAIT_TIMEOUT` (wait for gluetun's
healthcheck) and `DNS_WAIT_TIMEOUT` (poll for DNS to stabilize). Those are loops;
`TIMEOUT` is each individual request inside them.

**How the monitor "hunts" for a good result — and why `WGET_TRIES` defaults to 1.**
Reliability comes from *breadth and repetition over time*, not from retrying a
single request: gluetun tests the **whole site set** each loop and only acts after
`FAIL_THRESHOLD` **consecutive** loops fail; each dependent tests **one shuffled
site per loop** (so over loops it covers many names) and only acts after
`DEPENDENT_CONTAINER_FAILURES` consecutive failures. That shuffle-and-threshold
design already absorbs a flaky site, so a single fast attempt (`WGET_TRIES=1`) is
the right default — raise it only if your links are lossy enough that one in-loop
retry meaningfully helps.

### Per-URL tunables

`TIMEOUT`/`WGET_TRIES` set the **defaults**, and they're right for almost every
site. Occasionally one canary is *slow but alive* — it answers, just not within
the global ceiling. (`wget --timeout` bounds each connect/read/DNS step
**individually**, not the whole request, so a server that opens the connection and
then takes its time sending headers trips the timeout even though it's up — you'll
see `Read error (Operation timed out) in headers`.) Timing it out triggers a
gluetun restart that can't fix anything external.

For exactly that case you can override the probe knobs **per URL**, in either
source, with a `|key=value` suffix after the URL:

```bash
# sites.conf — bare URLs are unchanged; add |key=value only where you need it
https://www.google.com
https://slow-api.example|timeout=25
https://flaky.example|timeout=20|tries=2
```

```yaml
# SITES env — comma between entries, pipe within an entry
- SITES=https://www.google.com,https://slow-api.example|timeout=25
```

- **The separator is `|`** so one syntax works in both the file *and* the
  comma-separated `SITES` env: a URL never contains a bare `|`, and it survives the
  CSV split.
- **Keys:** `timeout` (seconds) and `tries` (attempts) — the per-URL equivalents of
  `TIMEOUT`/`WGET_TRIES`. A site with no override inherits the globals, so nothing
  changes for the rest.
- **Forgiving:** an unknown key or non-positive value is **warned about and skipped**
  — the URL is still monitored on the defaults; a typo never drops a site. The
  warning fires **the same way at startup and on a live reload** (deduped on reload),
  so a bad edit made while running is never applied silently.
- File overrides are **re-read live** like the URLs themselves, and the reload
  detects a change to a site's **full config** — not just added/removed URLs but a
  changed `role`/`timeout`/`tries` on an existing one. Any edit is logged with the
  specific before→after (`Sites changed: https://x (role critical→advisory)`), and
  the resolved config is re-logged (`Per-URL probe overrides: …`), so you always get
  confirmation the edit took effect.

#### Editing `sites.conf` live

The compose file mounts a **directory** (`./config:/config:ro`) rather than the
single `sites.conf` file **on purpose**, and this is important for live reload:

> A single-file bind mount pins the file's **inode** at container start. Most
> editors (`vim` by default, `sed -i`, many IDEs) save by writing a temp file and
> renaming it over the original — which creates a **new inode**. With a single-file
> mount the container keeps reading the *old* inode, so your edit is invisible until
> you recreate the container — the live reload silently does nothing. A **directory**
> mount resolves `sites.conf` fresh on every read, so any editor's save is picked up
> live.

If you must use a single-file mount, edit **in place** (append, or truncate-and-
write) — or run `docker compose up -d --force-recreate` after an atomic-save edit.

> **Why only `timeout`/`tries`?** These are the only **per-request** knobs — they
> bound *this URL's* probe, so a per-URL value is meaningful. The other timeouts
> are not per-URL-overridable by design: `CHECK_INTERVAL` is the loop cadence, and
> `HEALTHY_WAIT_TIMEOUT`/`DNS_WAIT_TIMEOUT` are **gluetun-restart-scoped wait
> budgets** — they wait for the *gluetun container* to recover, which has nothing to
> do with any single site, so there's nothing to scope to a URL. See
> [Timeouts & retries](#timeouts--retries--one-model-everywhere) for the full split.

#### Site roles — `critical` (default) vs `advisory`

`timeout`/`tries` tune *how* a site is probed; `role` decides *what its failure
means*. It's the same `|key=value` syntax:

```bash
https://www.google.com                     # critical (default)
https://geo-blocked.example|role=advisory  # probed, but never restarts gluetun
https://slow-and-flaky.example|role=advisory|timeout=25
```

- **`critical`** — the default (a bare URL). Its failure counts toward restarting
  gluetun, exactly as before — so existing configs are unchanged.
- **`advisory`** — the site is still probed and its reachability recorded in the
  stats, but its failure **never** triggers a gluetun restart, and it's excluded
  from the flaky-site advisory (you've already acknowledged it). Startup logs it
  under `Per-URL probe overrides: … role=advisory`, and a failing advisory site
  shows in the heartbeat as `failing: host (advisory)`. If you want to *hear* when
  an advisory site goes unreachable, it emits an opt-in, edge-triggered alert at the
  **`activity`** notification tier (silent at the default `attention`) — announced
  once it's been unreachable `FAIL_THRESHOLD` checks, resolved when it's back. So
  you can watch a geo-blocked endpoint's reachability without it ever restarting the
  tunnel or paging you.

Use `advisory` for a site you want to **watch** but that can't be reached through
the VPN regardless of tunnel health — a geo-blocked/anti-VPN endpoint (a torrent
indexer, say). As a `critical` site it would restart gluetun every loop trying to
roll to an exit that can reach it, churning every dependent, when the tunnel is
fine. As `advisory` you keep the reachability signal without the pointless restarts.

**advisory vs. deleting:** advisory *keeps probing* — the reachability data is the
value, for a site whose status actually varies. If a site is permanently
unreachable or you stop caring, just **delete the line** — probing it forever
teaches nothing. Deleting isn't a role; it's what you do to an advisory site you've
finished with. An unknown `role=` value is warned about at startup and falls back
to `critical` (fail-closed — an unrecognized site still protects the tunnel).

Unlike `timeout`/`tries` — which take their **global** defaults from `TIMEOUT`/`WGET_TRIES`
and are overridable per URL — `role` is **per-site only**: there's no global
default-role setting, because "everything advisory" would leave nothing gating the
tunnel. The startup/reload defaults line spells out the effective globals in force,
role included: `… all sites use TIMEOUT=10s, WGET_TRIES=1, role=critical`.

#### Let the monitor suggest them — `--suggest-tunables`

You don't have to guess. The monitor already records how every site behaves
(latency percentiles, failure categories, restart effectiveness — see
[Site stats & flaky-site advisory](#site-stats--flaky-site-advisory))
and can read that history back as concrete recommendations.

Those stats are *lifetime* counters, which is what makes the advice well-evidenced — and what would otherwise make it immortal. So suggestions are gated on **recent evidence**: a site with no failure inside `ADVISORY_WINDOW` (24h) yields nothing, however damning its lifetime totals. Advice about an episode from last month is noise, not diagnosis.

```bash
docker exec gluetun-monitor gluetun-monitor --suggest-tunables
# (or, without a running container)
docker compose run --rm gluetun-monitor --suggest-tunables
```

It prints a ranked, evidence-backed list — the biggest restart driver first — with
the exact line to paste:

```
slow-api.example
  18 read-timeout failure(s) but answers within p99=2579ms / max=13544ms — slow,
  not dead; a 25s timeout keeps it a useful canary instead of triggering restarts
  → https://slow-api.example|timeout=25
```

When a longer timeout *wouldn't* help — a site whose restarts rarely clear it and
whose failures are DNS/connection (genuinely unreachable, not merely slow) — it
says so and points you at reviewing/removing it instead (or, if you want to keep
watching its reachability without it restarting the tunnel, marking it
[`role=advisory`](#site-roles--critical-default-vs-advisory)). The suggestions are
**advisory only**: the monitor never edits your config. The flaky-site advisory in
the logs and notifications carries the same suggestion inline when one applies.

### Site Test Success/Failure Logic

The monitor distinguishes between **connectivity failures** (VPN broken) and **site errors** (VPN working, site returned an error). The decision is **HTTP-response-first**, not exit-code-based:

- **Any HTTP response = PASS** — including 401/403/404/5xx. A status line proves DNS resolved, the connection traversed the tunnel, and a server answered, so the tunnel is up regardless of *what* the server said (Tenet 3 — a broken tunnel is not a sad website).
- **Only a failure to get any HTTP response = FAIL** — DNS failure, connection refused, TLS error, or timeout.

This is also why it's correct across wget implementations: gluetun ships **GNU wget** (HTTP errors → exit 6/8), but dependent containers commonly run **busybox wget** (exit 1 for *any* HTTP error). Keying on "did we get an HTTP status?" rather than on the exit code means a busybox dependent's harmless 404 is read as a PASS, not a spurious failure. The wget exit code is used only as a **fallback** when no HTTP status line was captured at all (GNU's `0/6/8` = "responded").

**Probe method:** the check is `wget --spider` — a **HEAD** request (headers only, no body). On a server that rejects HEAD (405, or no HEAD support) GNU wget falls back to a **GET**, but still as a spider check — the response body is never downloaded either way. busybox wget's `--spider` is HEAD too. So the method is HEAD by default and GET only as a fallback, never a full content fetch; classification keys on the response, not the method.

**Key insight:** If a site returns HTTP 403 Forbidden or 503 Service Unavailable, the VPN is working — the site just doesn't like the request. Only actual network/DNS/TLS/timeout failures indicate a VPN problem.

#### `FAIL_THRESHOLD`
Number of **consecutive** failures for a **critical** site before triggering a restart. This prevents restarts from transient network blips. (An `advisory` site — see [Site roles](#site-roles--critical-default-vs-advisory) — is probed but never triggers a restart regardless of this threshold.)

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

#### Dependent memory — surviving restarts and recreates together

Discovery alone has a blind spot: a dependent stranded by a gluetun *recreate* points at the **old** container id, so current-id discovery can no longer see it — and if the monitor itself restarted moments earlier, its in-memory record of that dependent is gone too. The monitor closes this with a small persistent sidecar (`MONITOR_STATE_FILE`, default `/logs/monitor-state.json`) remembering two things Docker forgets:

- every container id gluetun has run under (Docker ids are never reused, so a container whose `network_mode` points at a dead id from this list *provably* belonged to this gluetun), and
- the names of dependents it has managed.

Each loop the monitor scans **all** containers (including exited ones — a stranded dependent's own restart policy usually drives it to Exited) and **adopts** any container stranded on a dead former-gluetun id, healing it through the normal remediation path. A container stranded on a dead id the monitor has *never* seen as gluetun is only warned about, never touched — it might belong to some other network owner.

The file is best-effort and human-readable; deleting it (or a corrupt file) simply resets the memory. One bootstrap gap: on the very first run there is no history yet, so dependents that were *already* stranded before the monitor ever saw gluetun can't be confirmed — list them explicitly in `DEPENDENT_CONTAINERS` for that one recovery. Details in [ADR-0014](docs/adr/0014-durable-dependent-memory.md).

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
- **`AUTO_RECREATE=0`** — never recreate; log a loud alert line instead
  (restart-only recovery). ("alert" here, and the flaky-site "advisory" below,
  mean a **log line** — there is no external notification yet; that's on the
  [roadmap](docs/ROADMAP.md).)
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
typically stays down when it breaks), the **longest** such streak, a
**failure-reason breakdown** (dns / tls / timeout / connection / http-error /
other), **response-latency** of successful polls (avg/min/max + **p50/p90/p99**, so
you see median vs mean — a site getting slow often precedes it failing), how many
gluetun restarts it triggered and the **restart-effectiveness** (fraction of those
restarts that actually cleared it — a low number means it's the site, not the VPN),
and first-seen / last-good / last-failure timestamps. It's written **every loop, crash- and power-loss-safely** (temp file
+ fsync + atomic rename), survives monitor restarts, and is best-effort (a
missing/unwritable/corrupt file never blocks the monitor). A site removed from
`sites.conf` is kept for `STATS_RETENTION_DAYS` (default 90) then pruned.

The file also has a top-level **`monitor`** section with monitor-wide totals:
version, first-/last-started, current uptime, **total loops**, **total runtime**
(accumulated, excluding downtime), and cumulative **gluetun restarts**,
**dependent remediations**, and **advisories** raised.

When one site dominates the recent restarts, the monitor logs a **flaky-site
advisory** (once, not per loop):

```
[WARN] FLAKY SITE: https://dognzb.cr caused 17 of the last 22 gluetun restarts
over the last 24h — it may be flaky; consider reviewing or removing it from sites.conf
```

That's the signal to prune that site from `sites.conf` (re-read each loop — see the
[live-editing caveat](#sites--config_file--sites) on in-place edits vs. a recreate).
Tune with `ADVISORY_WINDOW`, `ADVISORY_MIN_RESTARTS`, and
`ADVISORY_DOMINANCE`. See [ADR-0008](docs/adr/0008-persistent-site-stats-and-advisory.md).

The **dependent-flapping advisory** is the same idea aimed at a *dependent* rather
than a site: a container that keeps needing remediation but won't stay healthy
self-heals every loop (a quiet `recovery` event) and would otherwise never reach a
human. When one is remediated `DEPENDENT_ADVISORY_MIN_REMEDIATIONS` times within
`DEPENDENT_ADVISORY_WINDOW`, it escalates to an `attention` alert:

```
[WARN] FLAPPING DEPENDENT: qbittorrent remediated 6 times in the last 24h
— it won't stay healthy; investigate
```

It's **count-based, not dominance-based** (each dependent is independent — there's
no shared gluetun to contend for), and the per-loop DEBUG logs already show *which*
sites/DNS failed each time, so the alert just points you at the right container to
investigate.

> **By design, the monitor does not auto-suppress a flaky site** — it keeps
> applying the cheap restart fix and escalates to you. (A future automatic
> back-off is possible; it would be opt-in.)

### Viewing the stats: `gluetun-monitor-stats`

The image ships a read-only command that renders the sidecar as a per-site matrix
(it reads the same file the monitor writes, using the same code, so the numbers
always match). It touches no Docker API and never mutates state — safe to run any
time:

```console
$ docker exec gluetun-monitor gluetun-monitor-stats
monitor v2.0.0  loops=205  runtime=2.0h  gluetun_restarts=0  remediations=0  advisories=0
tracking since 2026-06-02 14:24

site                     polls  fails  rate%   avg   p50   p90   p99   max  eff%  last_fail
-----------------------  -----  -----  -----  ----  ----  ----  ----  ----  ----  ---------
https://thepiratebay.org   319      0   0.00  2255  2191  2527  2679  3136   n/a  —
https://www.google.com     319      0   0.00   716   631   911  1238  1274   n/a  —
...
latency in ms; eff% = restart-effectiveness (n/a = no restarts triggered)
```

Sites are sorted by `p90` (worst tail first) by default; `--sort` accepts
`p90|p99|avg|max|p50|rate|polls|eff|name`. Add `--json` to emit the same data for
`jq`/dashboards, and `--file PATH` if your `STATS_FILE` lives elsewhere.

The latency columns show the **recent** window (last ~200 polls) by default. Add
`--lifetime` for **all-time** percentiles — these come from a bounded per-site
histogram (DDSketch-style, within 5% relative error at a few dozen buckets/site;
see [ADR-0009](docs/adr/0009-all-time-latency-histogram.md)) that records every
successful poll for the site's whole life, so you get a lifetime baseline rather
than just "recently." `--json` includes both windows (`latency_ms` and
`lifetime_latency_ms`). Exact avg/min/max are kept either way; only the percentiles
are approximate.

## Notifications

By default the monitor is **log-only**. Set `APPRISE_URLS` to also push events
out-of-band via [Apprise](https://github.com/caronc/apprise) — one library, 100+
backends (ntfy, Discord, Telegram, Slack, email, Pushover, Gotify, generic webhook,
…), all configured by URL. Unset = disabled, so this is fully opt-in.

```yaml
    environment:
      # One or more comma-separated Apprise URLs:
      - APPRISE_URLS=ntfy://ntfy.example.com/gluetun
      # - APPRISE_URLS=ntfy://host/topic,discord://webhook_id/webhook_token
      - NOTIFY_LEVEL=attention   # attention | recovery | activity | all (default attention)
```

### One dial: `NOTIFY_LEVEL`

The scope is a single cumulative dial keyed on **who has to act**, not on how scary a
line looks. Each level adds its own row to everything above it (ADR-0011):

| `NOTIFY_LEVEL` | You get | Events |
|---|---|---|
| **`attention`** *(default)* | only when **you** must act/decide | recovery/remediation failed, refused to start, **cannot probe the gateway**, **cannot probe a dependent**, **flaky-site advisory**, **dependent-flapping advisory** |
| `recovery` | + self-healed incidents | gluetun recovered, dependent remediated |
| `activity` | + non-fault changes | `sites.conf` reloaded, **advisory site unreachable / recovered** |
| `all` | + the firehose | per-loop checks, restart play-by-play |

So enabling notifications gets you **`attention` only** — the monitor stays silent
through every self-heal and pings you when it's actually stuck. Raise the dial to
hear more.

### No notification storms

Alerts are **edge-triggered**: an ongoing problem is announced **once, when it
starts** — not every 30-second loop it persists. `NOTIFY_REPEAT_INTERVAL` (in
**loops**, default `0`) controls reminders: `0` = announce once then stay silent
until it resolves; `N` = remind every `N` loops. When a problem **clears** you get a
resolve note (so you hear it's back); when its subject is **removed** (site dropped,
dependent excluded) you get a "no longer monitored" note instead (the alert is
retired, not recovered). This state persists to `NOTIFY_STATE_FILE`,
so restarting the monitor neither re-spams still-broken problems nor misses a
resolve (ADR-0012).

A resolve means the condition was **observed** to clear, not that a counter
momentarily dipped. In particular the "gluetun cannot recover" alert stays active
until the sites that triggered it actually pass again — a site that is unreachable
through an otherwise-healthy tunnel (e.g. geo-blocked from the current exit) keeps
the alert firing once, without a false "recovered" every restart cycle.

### Wedged dependents — escalation with the runbook attached

Most remediation failures are transient and the next loop's retry clears them. But
some states **cannot** self-heal — the canonical one is an unremovable parked twin
left by an interrupted recreate (a storage-driver `dataset is busy` refusal: the
force-killed container's process tree survived and pinned the mount). Retrying is
free but futile, and the failure looks identical every loop.

When the **same** remediation failure repeats `WEDGE_ESCALATE_AFTER` consecutive
times (default 3), the monitor declares the dependent **wedged**:

- the generic `dependent unhealthy` alert is superseded by a distinct
  **`dependent WEDGED`** alert carrying the exact error, the parked twin's inspect
  state, and the **operator runbook** — the alert alone is enough to act on;
- remediation attempts **back off** (doubling per failed attempt, capped at
  `WEDGE_BACKOFF_CAP` seconds, default 10 min) instead of hammering the daemon with
  a doomed removal every loop;
- **probing never stops** — the dependent is still checked every loop, so once the
  blocker is cleared (or the container recovers on its own) the monitor finishes
  the heal itself and resolves the alert. A failure that *changes* restarts the
  count: a new error is a new situation, not a deeper wedge.

One deployment note: **a wedged alert is only as good as the `APPRISE_URLS` behind
it**. In log-only mode (the default) every attention-tier alert — this one included
— is just a log line, and "escalation" means hoping someone tails the log. If you
rely on the monitor to summon you when it's stuck, configure notifications.

### Grouped, best-effort

A loop's surviving events are **rolled up into one digest** (so one cycle = at most
one notification), colored by the most-urgent tier present. Sending is best-effort
and **never affects monitoring** (Tenet 7): run off the loop bounded by
`NOTIFY_TIMEOUT`, any failure swallowed (logged at `DEBUG`). Apprise URLs carry
tokens and are **never logged** — and at startup the log states exactly what you
signed up for, including what stays silent.

Verify your setup without waiting for a real event:

```bash
docker exec gluetun-monitor gluetun-monitor --notify-test
```

### Self-hosted backend with a self-signed certificate?

A common homelab gotcha: Apprise **verifies TLS certificates by default**, so a
self-hosted backend (mail server, ntfy, Gotify, …) presenting a **self-signed or
private-CA** cert fails with a vague *"Connection error"* — even when the URL is
correct. The image trusts only the public CA bundle, so it can't trust a private cert.

Two fixes — append **`?verify=no`** to the URL to skip verification (simplest, fine for
a homelab box):

```
mailtos://user:pass@mail.lan?verify=no
ntfy://ntfy.lan/gluetun?verify=no
```

…or, to keep verification on, mount your CA so the container trusts it:

```yaml
volumes:
  - ./my-ca.crt:/usr/local/share/ca-certificates/my-ca.crt:ro
```

See [ADR-0011](docs/adr/0011-notification-tiers-and-rollup.md) (the dial + rollup) and
[ADR-0012](docs/adr/0012-alert-lifecycle.md) (the lifecycle) for the design, and the
full [Apprise URL list](https://github.com/caronc/apprise/wiki) for backends.

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
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
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
      - PUID=1000                      # run non-root (recommended); unset = root (drop-in)
      - PGID=1000
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
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
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
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
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

### A normal check cycle (DEBUG)
Every test line is tagged with the container it ran in: site tests run inside the
gluetun container (through the tunnel); each dependent logs its interface/route
check, then its viability result.
```
[2025-01-15 10:00:00] [CHECK] Start
[2025-01-15 10:00:02] [DEBUG] [gateway:gluetun] reach ok: https://www.google.com (HTTP 200, 769ms)
[2025-01-15 10:00:02] [DEBUG] [gateway:gluetun] reach ok: https://cloudflare.com (HTTP 200, 1768ms)
[2025-01-15 10:00:04] [DEBUG] [dependent:qbittorrent] link live: eth0,lo,tun0
[2025-01-15 10:00:04] [DEBUG] [dependent:qbittorrent] reach ok: cloudflare.com (HTTP 200) [wget]
[2025-01-15 10:00:04] [CHECK] End - Sleeping 30s
```

Each line reads `[<role>:<name>] <dim> <verdict>: <target> (<detail>)`. The
dimension is `link` (the L3 interface/route check) or `reach` (DNS + connectivity);
the verdict is `ok` / `fail` / `stranded` / `?` (couldn't verify). A healthy line
omits the failure counter — a failing one shows `[2/2 → restart]`.

Each test line is tagged `[gateway:<name>]` (the gluetun VPN container, where the
site tests run — through the tunnel) or `[dependent:<name>]`, so you can tell at a
glance what kind of container a line is about (`grep gateway:` / `grep dependent:jackett`).

### Gluetun connectivity failure + recovery
```
[2025-01-15 10:10:00] [WARN] [gateway:gluetun] reach fail: https://example.com (Network failure (DNS or connection)) [2/2 → restart]
[2025-01-15 10:10:00] [ERROR] [gateway:gluetun] restart triggered by: https://example.com
[2025-01-15 10:10:00] [WARN] Gluetun unhealthy → restarting
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
[2025-01-15 11:00:00] [WARN] qbittorrent netns moved (gluetun recreated) → recreate
[2025-01-15 11:00:00] [WARN] Recreating qbittorrent (re-homing netns onto gluetun 9f3c1a2b4d5e)
[2025-01-15 11:00:02] [INFO] qbittorrent recreated as 7a1b2c3d4e5f and started
[2025-01-15 11:00:04] [INFO] qbittorrent verified healthy after remediation
```

### Log rotation (both sinks)

The monitor logs to **two** places, and both are bounded so a long-running
watchdog never fills the disk:

- **The `/logs` file** is **size-rotated by the monitor itself** —
  `LOG_MAX_BYTES` (≈10 MB) × `LOG_BACKUP_COUNT` (5) ≈ 60 MB cap. Set
  `LOG_MAX_BYTES=0` to disable (e.g. if you run external `logrotate`).
- **The Docker/stderr stream** (`docker logs`) is **not rotated by Docker
  automatically** — its default `json-file` driver grows unbounded. The compose
  example sets a `logging:` cap (`max-size`/`max-file`) on the services; keep it,
  or configure rotation globally in `daemon.json`.

At `LOG_LEVEL=DEBUG` the per-site/per-dependent lines are verbose (good for
soak-testing); `INFO` is much quieter for steady-state.

### Running as non-root (recommended)

By default the container runs as **root** — a drop-in match for v1, with nothing to
configure. Running it **non-root is recommended** (defense in depth), and it uses
the same `PUID`/`PGID` knob the rest of your stack (the LinuxServer.io `*arr`
images) already does:

```yaml
services:
  gluetun-monitor:
    environment:
      - PUID=1000   # your host user's uid — run `id` to find it
      - PGID=1000   # your host user's gid
```

When `PUID`/`PGID` are set, the entrypoint chowns the `/logs` mount to that user
and drops privileges to it — **no manual chown**. Unset, it stays root and behaves
exactly like v1 (so the upgrade is drop-in; non-root is opt-in). Either way the
monitor's real privilege is the Docker API it talks to, not its in-container uid.

> **Direct socket mount** (instead of the recommended socket proxy) **+ non-root:**
> a non-root process can't read the root:docker-owned `/var/run/docker.sock`. With
> the socket **proxy** (the default) this is a non-issue — it's reached over TCP.
> If you insist on the direct mount *and* non-root, use Docker's native
> `user: "<uid>:<gid>"` together with `group_add: ["<docker-gid>"]` instead of
> `PUID`/`PGID` (the privilege-drop resets supplementary groups, so `group_add`
> only composes with `user:`), or simply leave it as root there.

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

All three are required. `POST=1` in particular is unavoidable: tecnativa's `POST` is a *binary* switch for the container API — with `POST=0`, the `EXEC` and `ALLOW_RESTARTS` carve-outs are inert, so neither probing nor restarting works (verified; see [#29](https://github.com/csmarshall/gluetun-monitor/issues/29)).

**If the monitor cannot probe, it does nothing.** Should `EXEC` be missing, the proxy become unreachable, or Gluetun stop shipping a usable `wget`, the site probes cannot *run* — which says nothing about the tunnel. Rather than mistake that for a connectivity failure and restart Gluetun (a restart cannot restore an EXEC permission), the monitor raises a distinct `attention` alert — *"cannot probe `<gluetun>`"* — holds its existing alerts, touches nothing, and never claims recovery it could not verify ([#137](https://github.com/csmarshall/gluetun-monitor/issues/137)).

**The same holds for a dependent it cannot see into** — and "cannot see into" includes a container that is *crash-looping*, because Docker reports one as `Running: true` throughout (`Running` means the daemon considers it alive, not that it works). Such a dependent is never counted as healthy and never remediated: restarting a container that is already restarting fixes nothing, and would bounce the network namespace its siblings share. It is reported instead — excluded from the healthy count but never hidden from the total (`dependents: 3/4 ok (1 unprobeable)`), and escalated to a *"cannot probe `<dependent>`"* `attention` alert if it stays unprobeable, which resolves itself the moment the container answers again ([#147](https://github.com/csmarshall/gluetun-monitor/issues/147)). A dependent *stranded* by a Gluetun recreate also crash-loops — its restart policy keeps starting it onto a dead namespace — so the strand check runs first and still heals it; only a container dying of its own accord is left alone.

On `wget` specifically: Gluetun installs GNU `wget` deliberately (it's an explicit `apk add` in its Dockerfile), but that is an *implementation detail* rather than a documented guarantee — whether its presence is part of Gluetun's supported surface is [an open question upstream](https://github.com/passteque/gluetun/discussions/3387). The monitor is built not to care: probes are classified on the **HTTP response**, not on `wget`'s exit code, so GNU and BusyBox `wget` behave identically — and if it ever disappears entirely, the paragraph above applies. We report; we do not restart.

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
