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
>
> Coming from the bash v1 (`:1`)? **v1 is end-of-life and does not actually
> recover** — see [Upgrading from v1](docs/UPGRADING-V1.md).

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

## Documentation

| Guide | What's in it |
|---|---|
| **[Configuration](docs/CONFIGURATION.md)** | Every environment variable, per-URL tunables (`timeout` / `tries` / `role`), the one timeout model, site pass/fail logic, dependent discovery, running as non-root |
| **[Docker Compose examples](docs/COMPOSE-EXAMPLES.md)** | Minimal (socket proxy), full (every option), and direct-socket-mount compose files |
| **[Site stats & notifications](docs/NOTIFICATIONS.md)** | The persistent stats sidecar, `gluetun-monitor-stats`, the flaky-site and dependent-flapping advisories, Apprise setup and `NOTIFY_LEVEL` |
| **[Architecture](docs/ARCHITECTURE.md)** | How `network_mode: "container:"` sharing strands a dependent, dependent-aware health (#20), auto-discovery, and the Docker socket proxy |
| **[Log output](docs/LOG-OUTPUT.md)** | Annotated samples: startup, a normal cycle, a failure and recovery, a self-healed strand, log rotation |
| **[Upgrading from v1](docs/UPGRADING-V1.md)** | v1 is end-of-life and does not actually recover (#81). Image tags, the two behavior changes, rollback |
| **[Compatibility](docs/COMPATIBILITY.md)** | What each capability your dependent ships unlocks — and what a `FROM scratch` container still gets |
| **[Tenets](docs/TENETS.md)** and **[ADRs](docs/adr/)** | The design record: what the monitor will never do, and why each decision was made |
| **[Versioning](docs/VERSIONING.md)** | Image tag policy and the release process |

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
[Editing sites.conf live](docs/CONFIGURATION.md#editing-sitesconf-live) for why this matters.

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
required — see [Configuration](docs/CONFIGURATION.md). The monitor must reach a real
endpoint set to do its job, so it refuses to start with none.

### 4. Deploy

```bash
docker compose up -d
```

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
  (restart-only recovery). ("alert" here, and the flaky-site "advisory", are log
  lines by default; set `APPRISE_URLS` to also push them out-of-band — see
  [Notifications](docs/NOTIFICATIONS.md).)
- **`EXCLUDE_CONTAINERS=...`** — a denylist of containers to never touch.
- **Socket proxy** (default) — cap the Docker API surface the monitor can use.

## Requirements

- Docker with API access (via [socket proxy](docs/ARCHITECTURE.md#docker-socket-proxy) or direct socket mount)
- Gluetun container with a healthcheck configured — its own is ideal, and [shouldn't be overridden](docs/ARCHITECTURE.md#gluetuns-healthcheck--dont-override-it)
- Dependent containers using `network_mode: "container:<gluetun>"`

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
