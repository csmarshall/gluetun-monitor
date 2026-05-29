# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2026-05-28

### Changed
- **Reimplemented in Python** (docker-py SDK) — see
  [ADR-0007](docs/adr/0007-reimplement-in-python.md). The connectivity test is
  unchanged in behavior (still `wget --spider` from inside gluetun's netns, same
  exit-code map); the rewrite is about making the now-complex orchestration
  testable. A characterization/differential suite pins the v1.x bash contract and
  the Python port matches it green.
- Image base is now `python:3.13-slim` (docker-py talks the Docker API directly;
  the `docker` CLI is no longer needed in the image). The socket-proxy
  permissions are unchanged (`CONTAINERS`/`POST`/`EXEC`).
- DEBUG logs are now gated behind `LOG_LEVEL` (default `INFO`); the v1.x log
  format is otherwise byte-for-byte preserved.

### Added
- **Dependent-aware health (issue #20).** The monitor no longer reports healthy
  when dependents (`network_mode: service:gluetun`) are stranded loopback-only
  after a gluetun restart/recreate. Each loop it interface-checks every dependent
  and runs a per-dependent connectivity + DNS viability probe (one shuffled
  resolvable name per dependent). See ADR-0004 / ADR-0006.
- **Self-healing for stranded dependents.** Same-instance strand → `docker
  restart`; gluetun recreated under it (id changed) → **non-destructive
  recreate** (volumes preserved, including anonymous volumes). See ADR-0005.
- New env vars: `DEPENDENT_CONTAINER_FAILURES` (default = `FAIL_THRESHOLD`),
  `MAX_PARALLEL_CHECKS` (default 6), `AUTO_RECREATE` (default on),
  `DNS_WAIT_TIMEOUT` (default 30), `LOG_LEVEL` (default `INFO`).
- **`SITES` env var** — comma-separated test URLs, **unioned** (de-duplicated)
  with `sites.conf`, for config-via-env parity with the other knobs. Either
  source works; at least one site total is required. `sites.conf` is re-read each
  loop (live-editable); `SITES` is fixed at startup.

### Changed (behavior)
- **Configuration is now validated; bad config is fatal (exit non-zero) instead
  of guessed around.** A malformed env value (bad int/bool/`LOG_LEVEL`), no
  testable sites (neither `sites.conf` nor `SITES`), or an explicit
  `DEPENDENT_CONTAINERS` naming a container that doesn't exist, all cause the
  monitor to refuse to start. v1 silently defaulted / warned-and-skipped / ran
  green-while-testing-nothing; these are now loud, fatal misconfigurations. Sane
  defaults still mean an *unset* var just uses its default — only a *set-but-bad*
  value is fatal. (`DEPENDENT_CONTAINERS=auto` discovering zero is not an error.)

### Migration
- Pull `:2` / `2.0.0`. Existing env / `sites.conf` / compose work unchanged. To
  roll back, repin the v1.x image (`:1` / `1.1.1`). The bash implementation
  remains in the repo as the differential oracle and rollback anchor.

## [1.1.1] - 2026-05-16

### Fixed
- Quotes in VPN endpoint location strings no longer crash the monitor
  (`xargs: unmatched single quote`). Whitespace trimming now uses a pure-bash
  `trim` helper instead of `xargs`, which interprets quotes as shell quoting.
  Affects any region containing an apostrophe, e.g.
  `Provence-Alpes-Cote-d'Azur`. (#17)

## [1.0.0] - 2025-12-12

### Added
- Initial release
- Multi-site parallel connectivity testing through Gluetun
- Auto-discovery of dependent containers via Docker socket
- Automatic Gluetun restart on connectivity failure (forces new VPN endpoint)
- Automatic restart of dependent containers after recovery
- VPN endpoint logging (IP, country, city, server)
- Configurable failure threshold before restart
- DNS stabilization wait after Gluetun restart
- Connectivity verification before restarting dependents
- Smart failure detection (HTTP 4xx/5xx = VPN working, network errors = failure)
- Comprehensive documentation (README, DEVELOPMENT.md)
- Docker Compose deployment
- MIT License

### Technical Details
- Pure bash implementation (no external dependencies beyond Docker CLI)
- Parallel site testing using background jobs
- Uses wget --spider for memory-efficient header-only requests
- Docker socket integration for container management
- Shellcheck clean
