# Development Notes

Context for working on gluetun-monitor. The **why** behind the architecture
lives in [`docs/`](docs/): start with [`docs/TENETS.md`](docs/TENETS.md), then
the ADRs ([`docs/adr/`](docs/adr/)) and the per-loop state machine in
[ADR-0006](docs/adr/0006-per-dependent-viability-testing.md). This file covers
the **how**: layout, toolchain, and testing.

## v2 at a glance

v2 (ADR-0007) is a Python package, `gluetun_monitor`. It does everything v1.x did
and adds dependent-aware health + self-healing (issue #20). The legacy
`gluetun-monitor.sh` remains in the tree as the **differential oracle** and
rollback anchor (`:1` image) — not for further feature work.

The single most important design idea: **all Docker access goes through one
seam** (`docker_client.DockerClient`, implemented over docker-py). Tests inject a
`FakeDockerClient`, so the entire monitor — including the destructive-adjacent
recreate path — is exercised without a live daemon.

## Package layout

```
gluetun_monitor/
├── __main__.py       # python -m gluetun_monitor
├── cli.py            # main(): config + logger + client, prereqs, run loop
├── config.py         # Config dataclass, from_env() (the env-var contract)
├── logging_setup.py  # stdlib logging, formatted to the v1.x line format
├── docker_client.py  # the seam: DockerClient Protocol + DockerPyClient + ContainerInfo
├── sites.py          # sites.conf parsing (+ per-URL |role/timeout/tries), IP-vs-host
├── connectivity.py   # probe_site() — wget --spider, HTTP-response-first classification
├── dns_check.py      # per-dependent DNS validation (wget→getent→ping cascade)
├── endpoint.py       # parse gluetun logs for IP/location/server (issue #17 safe)
├── dependents.py     # discovery, interface check, restart-vs-recreate decision
├── recreate.py       # build_create_body() (pure) + recreate_dependent() — ADR-0005
├── recovery.py       # gluetun restart waits + dependent remediation/verify
├── monitor_state.py  # durable dependent memory: gluetun-id history + known deps (ADR-0014)
├── site_stats.py     # persistent per-site stats + flaky-site advisory (ADR-0008)
├── histogram.py      # bounded DDSketch-style latency percentiles (ADR-0009)
├── tunables.py       # --suggest-tunables: per-URL timeout/tries recommendations
├── report.py         # gluetun-monitor-stats CLI (human/JSON stats view)
├── notify.py         # opt-in Apprise notifier: tier filter + rollup (ADR-0010/0011)
├── alert_state.py    # edge-triggered alert lifecycle + persistence (ADR-0012)
├── state.py          # consecutive-failure Counter + enums
└── monitor.py        # Monitor: the per-loop state machine (ADR-0006 nodes 1-22)
```

## Toolchain

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

ruff check gluetun_monitor tests   # lint (replaces shellcheck for the Python)
mypy gluetun_monitor               # types — strict
pytest                             # unit + differential
pytest --cov=gluetun_monitor --cov-report=term-missing
```

CI runs all three on every PR (`.github/workflows/ci.yml`), plus shellcheck and
the legacy bats suite against `gluetun-monitor.sh`, plus a Docker build +
container-start integration check.

The runtime Python version is single-sourced from the Dockerfile's `FROM` line, so
CI can never test against a version the image doesn't ship. Moving it to a new minor
(3.14 → 3.15 → …) is a short checklist: [`docs/PYTHON-BUMP.md`](docs/PYTHON-BUMP.md).

## Testing strategy

- **Unit tests** (`tests/test_*.py`) — each module against the `FakeDockerClient`
  (`tests/fakes.py`). State mutation in the monitor is single-threaded, so these
  are deterministic; the shuffle RNG is injected (`random.Random(0)`).
- **Recreate tests** (`tests/test_recreate.py`) — the highest-risk logic. The
  field-stripping and the **anonymous-volume data-preservation** guarantee are
  pinned because `build_create_body` is a pure function.
- **Characterization / differential** (`tests/test_characterization.py`, marked
  `differential`) — executes the *actual* legacy bash functions and asserts the
  Python port matches (`trim`, `decode_wget_exit_code`, env defaults). This is
  the no-regressions gate from ADR-0007. Run just these with
  `pytest -m differential`.

### Manual / live testing

```bash
# Connectivity through gluetun (what probe_site does):
docker exec gluetun wget --spider -S --timeout=10 --tries=1 -q https://google.com; echo $?

# A dependent's interface check (what the interface check does):
docker exec <dependent> ls /sys/class/net      # only "lo" => stranded

# A dependent's NetworkMode target vs gluetun's id (the recreate selector):
docker inspect --format='{{.HostConfig.NetworkMode}}' <dependent>
docker inspect --format='{{.Id}}' gluetun
```

### Simulating the #20 strand

```bash
docker restart gluetun     # same id -> dependents stranded, recover via restart
docker compose up -d --force-recreate gluetun   # new id -> recover via recreate
```

## Conventions

- `mypy --strict` and `ruff` must stay green; keep public functions typed +
  docstring'd.
- New env vars: add to `Config`, document in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)
  (the variable table **and** a Variable Details entry if it needs one), and (if it
  changes the contract) extend the characterization suite.
- Docker behavior is added to the `DockerClient` Protocol **and** the
  `FakeDockerClient`, never reached for directly — that keeps everything testable.
- Significant/hard-to-reverse decisions get an ADR; tunable heuristics go in
  `TENETS.md` or the code (see [`docs/adr/README.md`](docs/adr/README.md)).

## Backlog

The notification layer and socket-proxy hardening are tracked in
[`docs/ROADMAP.md`](docs/ROADMAP.md).
