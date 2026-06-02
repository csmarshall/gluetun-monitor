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
  `DNS_WAIT_TIMEOUT` (default 30), `LOG_LEVEL` (default `INFO`),
  `DEPENDENT_VIABILITY` (default on; `0` = interface/strand check only, no L7
  probe), `MAX_JITTER_MS` (default 0; opt-in per-dispatch jitter),
  `DRY_RUN` (default off; observe-only — detect + log intended actions but never
  restart/recreate, for soak-testing alongside an active monitor).
- **`SITES` env var** — comma-separated test URLs, **unioned** (de-duplicated)
  with `sites.conf`, for config-via-env parity with the other knobs. Either
  source works; at least one site total is required. `sites.conf` is re-read each
  loop (live-editable); `SITES` is fixed at startup.
- **`EXCLUDE_CONTAINERS` env var** — comma-separated denylist of containers to
  never manage. Filters auto-discovery and subtracts from an explicit list. On
  overlap with `DEPENDENT_CONTAINERS`, exclude wins with a warning ("first, do no
  harm"); an exclude name matching nothing warns (likely typo).
- **Startup warning for stranded orphans** — a running container whose netns
  parent no longer exists (likely a dependent recreate-stranded before the
  monitor started) is logged at WARN with a pointer to `DEPENDENT_CONTAINERS`. It
  is not auto-recreated — an orphan whose parent is gone can't be confirmed as a
  gluetun dependent (Tenet 1).
- **Persistent per-site stats + flaky-site advisory** ([ADR-0008](docs/adr/0008-persistent-site-stats-and-advisory.md)).
  A human-readable JSON sidecar (`STATS_FILE`, default `/logs/site-stats.json`,
  best-effort, survives restarts) records per test site: total polls/failures
  (→ rate), failure episodes + average episode length, gluetun restarts triggered,
  and last-good/last-failure timestamps. When one site dominates the recent
  gluetun restarts the monitor logs a once-per-episode **flaky-site advisory**
  ("X of the last Y restarts were this site over the last <window> — review it").
  By design it still applies the cheap restart fix and escalates to a human rather
  than auto-suppressing the site. Knobs: `ADVISORY_WINDOW`, `ADVISORY_MIN_RESTARTS`,
  `ADVISORY_DOMINANCE`, `STATS_SAVE_EVERY`.

### Fixed
- **Per-dependent viability no longer false-fails on busybox-wget dependents.**
  The probe classified results by GNU wget's exit codes (0/6/8 = responded), but
  dependent containers commonly run **busybox wget** (linuxserver/Alpine images),
  which returns exit 1 for any HTTP error response — so a harmless 404/403 from a
  dependent was misread as a failure (and could trigger a spurious restart).
  Classification is now HTTP-response-first and, for dependents, keys on **DNS
  resolution** only: any HTTP response or a resolved-but-unreached site counts as
  viable; only a real DNS-resolution failure does not (dependents share gluetun's
  netns, so DNS is the sole per-container fault — strands are caught by the
  interface check). Failures now log wget's actual reason instead of a bare
  "Generic error".
- **Portable DNS validation via a getaddrinfo cascade** (`wget → getent → ping`)
  so the check survives a dependent lacking any one tool, and resolves the way
  the application does (nslookup/dig are excluded — they bypass nsswitch/libc and
  can lie). When a container has *no* usable resolver tool (e.g. distroless), DNS
  is reported **UNVALIDATED** — logged once, falling back to the interface check,
  rather than silently passing.

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
- **v1 (the bash implementation, image `:1`) is end-of-life; move to v2.** The
  upgrade is a drop-in config change: same env vars/defaults, same
  `sites.conf`/`logs` paths, same socket-proxy permissions
  (`CONTAINERS`/`POST`/`EXEC`). In the common case you change only the image tag.
- Recommended tag for production is **`:2`** (all v2.x patches, no surprise
  major); `:latest` floats; `:1` stays as the rollback anchor (one-step rollback:
  repin `:1`).
- Two behavior changes to expect: (1) v2 **heals dependents by default**
  (restart/recreate, volumes preserved) — set `AUTO_RECREATE=0` and/or
  `DEPENDENT_VIABILITY=0` to stay conservative; (2) **bad config is now fatal** —
  an empty `sites.conf`, a malformed env value, or an explicit
  `DEPENDENT_CONTAINERS` naming a missing container will refuse to start with a
  clear error (v1 tolerated these silently).

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
