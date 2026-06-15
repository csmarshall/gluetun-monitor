# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.4](https://github.com/csmarshall/gluetun-monitor/compare/v2.1.3...v2.1.4) (2026-06-15)


### Fixed

* **deps:** rebuild on updated python:3.14-slim base (sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061) ([16c2c58](https://github.com/csmarshall/gluetun-monitor/commit/16c2c58161ecbf85803605ee072538f1c7e57450))

## [2.1.3](https://github.com/csmarshall/gluetun-monitor/compare/v2.1.2...v2.1.3) (2026-06-08)


### CI / tooling

* bump googleapis/release-please-action from 4 to 5 ([#57](https://github.com/csmarshall/gluetun-monitor/issues/57)) ([32b7388](https://github.com/csmarshall/gluetun-monitor/commit/32b7388f76bc17ff120b4ee1097847902e21d38b))
* skip auto-assign for Dependabot PRs (restricted token 403s; they self-assign) ([#58](https://github.com/csmarshall/gluetun-monitor/issues/58)) ([e2b959d](https://github.com/csmarshall/gluetun-monitor/commit/e2b959d9cda8329faf86d8c15b672769f42664c0))

## [2.1.2](https://github.com/csmarshall/gluetun-monitor/compare/v2.1.1...v2.1.2) (2026-06-06)


### CI / tooling

* weekly base-image drift check — auto-rebuild stable on base security patches (ADR-0013, [#26](https://github.com/csmarshall/gluetun-monitor/issues/26)) ([#53](https://github.com/csmarshall/gluetun-monitor/issues/53)) ([8976e50](https://github.com/csmarshall/gluetun-monitor/commit/8976e50a877a23ca1491a9f01c54db86fefa54b7))

## [2.1.1](https://github.com/csmarshall/gluetun-monitor/compare/v2.1.0...v2.1.1) (2026-06-05)


### CI / tooling

* drop Python 3.13, go 3.14-only ([#52](https://github.com/csmarshall/gluetun-monitor/issues/52)) ([6ca58a4](https://github.com/csmarshall/gluetun-monitor/commit/6ca58a4e80eed358c5a6ec6e857d39c4da0f655d))
* fix YAML parse error in auto-merge-release (quote the label if-expr) ([#51](https://github.com/csmarshall/gluetun-monitor/issues/51)) ([b27b1ce](https://github.com/csmarshall/gluetun-monitor/commit/b27b1ce18b169f6144b7d4966bcc9c19b0482b19))
* hashed runtime lockfile for deterministic, integrity-checked image builds (ADR-0013, [#26](https://github.com/csmarshall/gluetun-monitor/issues/26)) ([#47](https://github.com/csmarshall/gluetun-monitor/issues/47)) ([b5054d9](https://github.com/csmarshall/gluetun-monitor/commit/b5054d9d81da0ecc9235986215711d26c1c7e0a5))
* release-please foundation — accumulating Release PR (ADR-0013, [#26](https://github.com/csmarshall/gluetun-monitor/issues/26)) ([#48](https://github.com/csmarshall/gluetun-monitor/issues/48)) ([3c72f6f](https://github.com/csmarshall/gluetun-monitor/commit/3c72f6fba70a341fde79174ab99f213a16965119))
* rolling :edge channel + SLSA provenance/SBOM on images (ADR-0013, [#26](https://github.com/csmarshall/gluetun-monitor/issues/26)) ([#46](https://github.com/csmarshall/gluetun-monitor/issues/46)) ([58252e8](https://github.com/csmarshall/gluetun-monitor/commit/58252e83ebbb1647212e06ae16862f9f28401f58))
* wire release-please into the pipeline — Release-PR authorship auto-merge + release.yml reconcile (ADR-0013) ([#50](https://github.com/csmarshall/gluetun-monitor/issues/50)) ([17d958b](https://github.com/csmarshall/gluetun-monitor/commit/17d958b157aa879f4461bee1490e85c89f52e89d))

## [Unreleased]

### Added
- Rolling **`:edge`** image tag — rebuilt on every push to `main` (bleeding edge, not
  guaranteed stable; see `docs/VERSIONING.md`), plus an addressable `:edge-<sha>` per
  build. Decoupled from releases: `:latest`/`:MAJOR` still move only on a cut release.
- Published images now carry **SLSA provenance + an SBOM** attestation — a supply-chain
  trail (and the source the planned base-image drift check will read). (ADR-0013)

### CI / tooling
- The image now installs runtime deps from a fully-pinned, **hashed `requirements.lock`**
  (pip-compile) instead of resolving them at build time — a deterministic,
  integrity-checked dependency tree and a stable Python layer for the planned drift
  check. A CI guard fails if the lock's direct pins fall out of sync with
  `pyproject.toml`, so a dep bump can never silently miss the image. (ADR-0013)

## [2.1.0] - 2026-06-05

### Added
- **Opt-in notification layer (#22, ADR-0010/0011/0012).** Set `APPRISE_URLS` to push
  events out-of-band via [Apprise](https://github.com/caronc/apprise) (100+ backends:
  ntfy/Discord/Telegram/email/webhook/…). Unset = disabled (drop-in, no behavior
  change). `gluetun-monitor --notify-test` verifies your config.
  - **One dial, `NOTIFY_LEVEL`** (default `attention`), cumulative and keyed on
    actionability: `attention` (only when you must act — failed recovery/remediation,
    refused start, flaky-site advisory) → `recovery` (self-healed incidents) →
    `activity` (non-fault changes) → `all` (firehose).
  - **Per-loop rollup:** a cycle's events are grouped into one digest, colored by the
    most-urgent tier — no storms from a fast restarter.
  - **Edge-triggered lifecycle:** an ongoing problem announces once, reminds every
    `NOTIFY_REPEAT_INTERVAL` loops (default `0` = once), and emits a resolve when it
    clears (or a "no longer monitored" note if its subject was removed). State
    persists to `NOTIFY_STATE_FILE` across monitor restarts.
  - Best-effort (Tenet 7): sent off the loop bounded by `NOTIFY_TIMEOUT`, failures
    swallowed; URLs never logged; startup logs exactly what you signed up for.

### CI / tooling
- Pinned dev toolchain in `requirements-dev.txt` (ruff/mypy/pytest/pytest-cov),
  installed by CI for reproducible runs and tracked per-release by Dependabot (#23).
- Scoped Dependabot auto-merge: low-risk bumps (dev tooling + github-actions,
  patch/minor only) auto-merge **after** the full matrix passes; majors, the
  runtime `docker` lib, and Docker base-image bumps still require human review (#23).
- `pip-audit` now upgrades build tooling (pip/setuptools/wheel) before scanning, so
  a fresh advisory against the runner's own bundled pip can't fail the build on
  something we don't ship; only our real dependency tree gates CI.
- Real-daemon integration test + CI job (#24): drives the actual `DockerPyClient`
  (exec/inspect/restart + the non-destructive recreate, asserting a dependent's
  volume data survives) against a live `dockerd`, so a docker-py regression turns CI
  red. The unit job now deselects the `integration` marker.
- Promoted the runtime `docker` lib into Dependabot auto-merge (patch/minor), now
  that the real-daemon job (a required check) gates it. Pinned `docker==7.1.0` in
  `pyproject.toml` so the image build is reproducible and Dependabot tracks it
  per-release; majors (8.x) still require a human.
- `apprise` ships pinned (`==1.11.0`) and validated by a real-library localhost-sink
  test (a required check), so it joins `docker` as an auto-merge-eligible prod dep
  (patch/minor); majors still require a human (#22).

### Dependencies
- Bump `ruff` 0.15.15 → 0.15.16 (auto-merged).
- Bump `dependabot/fetch-metadata` v2 → v3 (Node 24 runtime; outputs unchanged).

## [2.0.1] - 2026-06-04

### Changed
- Image base bumped to **python:3.14-slim**; CI now tests the supported range on a
  **3.13 + 3.14 matrix** (we declare `requires-python >=3.13`). Behavior-compatible.

### Security
- Strip control characters from the country/city parsed out of gluetun's endpoint
  logs before logging them (#30). That geo string comes from a third-party
  IP-getter, so control chars / ANSI escapes in a malicious response could
  otherwise reach the log file (cosmetic terminal-injection); Unicode place names
  are preserved.

### CI / tooling
- Dependabot now tracks the `pip` ecosystem; CI gained a `pip-audit` step;
  `actions/setup-python` bumped to v6.

### Docs
- Clarified the `sites.conf` live-reload contract (#33): "re-read every loop"
  reloads reliably only for **in-place** edits; with a single-file bind mount, an
  editor/tool that saves via rename replaces the inode and the container keeps
  reading the old file until `--force-recreate`. Documented the workaround and the
  **directory-mount** option (`./config:/config`) for guaranteed live-reload.
- Documented the probe-method contract (#34): `wget --spider` is a HEAD request
  (GET fallback only on HEAD-hostile servers; the body is never downloaded).
- README "How It Works" is now a mermaid flowchart (looping back to the start);
  the env-var table rows link down to their Variable Details.
- Pinned the `:latest` contract in VERSIONING.md (`latest=auto`; EOL/older-major
  patches never claim `:latest`).

## [2.0.0] - 2026-06-03

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
  probe), `DEPENDENT_VIABILITY_SAMPLES` (default 1; sites each dependent tests per
  loop — `N` or `-1` for all), `MAX_JITTER_MS` (default 0; opt-in per-dispatch jitter),
  `DRY_RUN` (default off; observe-only — detect + log intended actions but never
  restart/recreate, for soak-testing alongside an active monitor),
  `WGET_TRIES` (default 1; attempts per `wget` probe), `LOG_MAX_BYTES` /
  `LOG_BACKUP_COUNT` (log rotation), and `LOG_FILE` (log path; also always to stdout).
- **One standardized `TIMEOUT` (and `WGET_TRIES`) across every probe** — the same
  per-request timeout/retries now apply identically to gluetun's site tests, the
  dependent-container viability `wget`, and the post-restart DNS-readiness probe
  (which previously hard-coded a 5 s timeout). Set `TIMEOUT=10` once and it reaches
  the `wget` run *inside* the dependents too.
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
  than auto-suppressing the site. Per-site metrics also include the **longest
  failure streak**, a **failure-reason breakdown** (dns/tls/timeout/connection/
  http-error/other), **response-latency** of successful polls (avg/min/max +
  **p50/p90/p99**), and **restart-effectiveness** (fraction of a site's restarts
  that actually cleared it — a site-vs-VPN signal). A top-level **`monitor`**
  section records monitor-wide totals (version, uptime, total loops, accumulated
  runtime, cumulative gluetun restarts / dependent remediations / advisories).
  The file is written
  every loop, crash/power-loss-safely (temp file + fsync + atomic rename), and a
  site removed from `sites.conf` is pruned after `STATS_RETENTION_DAYS` (default
  90). Knobs: `ADVISORY_WINDOW`, `ADVISORY_MIN_RESTARTS`, `ADVISORY_DOMINANCE`,
  `STATS_RETENTION_DAYS`.
- **`gluetun-monitor-stats` command** — a read-only operator command (shipped in
  the image, `docker exec gluetun-monitor gluetun-monitor-stats`) that renders the
  stats sidecar as a per-site matrix (latency percentiles, failure rate,
  restart-effectiveness) plus monitor-wide totals. Sortable (`--sort`), with a
  `--json` mode for jq/dashboards. Reads the same file via the same code the
  monitor uses, so the numbers always match; touches no Docker API.
- **All-time latency percentiles** alongside the recent window. A per-site
  bounded **histogram** (DDSketch-style, [ADR-0009](docs/adr/0009-all-time-latency-histogram.md))
  records every successful poll's latency for the site's whole life — within 5%
  relative error at a few dozen buckets/site — so you get a *lifetime* baseline,
  not just the last 200 samples (the recent ring). Exact count/avg/min/max are
  kept too. View it with `gluetun-monitor-stats --lifetime`; `--json` includes both
  windows. Survives restarts (mergeable bucket counts) and is best-effort like the
  rest of the sidecar.
- **Concise, consistent per-loop log grammar.** Each line reads
  `[<role>:<name>] <dim> <verdict>: <target> (<detail>) [tool] [n/threshold → action]`.
  The dimension is `link` (the L3 interface/route check, shown *before* the
  connectivity test) or `reach` (DNS + connectivity), unifying the gateway site
  test and the dependent viability test under one greppable verb; the verdict is
  `ok` / `fail` / `stranded` / `?`. Healthy lines omit the failure counter (a
  failing one shows e.g. `[2/2 → restart]`), and the detail is the bare proof
  (`HTTP 200`, `bad address`) rather than a redundant restatement. Every line is
  tagged with the container's role + name — `[gateway:<gluetun>]` (site tests, run
  through the tunnel) or `[dependent:<name>]`.
- **Log files are rotated** so the watchdog can't fill its own disk: the `/logs`
  file is size-capped (`LOG_MAX_BYTES` ≈10 MB × `LOG_BACKUP_COUNT` 5; `0`
  disables). The compose example also caps the Docker/stderr stream
  (`logging: max-size/max-file`), which Docker does *not* rotate on its own.
- **Log files are rotated** so the watchdog can't fill its own disk: the `/logs`
  file is size-capped (`LOG_MAX_BYTES` ≈10 MB × `LOG_BACKUP_COUNT` 5; `0`
  disables). The compose example also caps the Docker/stderr stream
  (`logging: max-size/max-file`), which Docker does *not* rotate on its own.

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

### Security & robustness
- **Optional non-root via `PUID`/`PGID`** (LinuxServer.io-style). Set them and the
  entrypoint chowns `/logs` and drops privileges to that uid/gid — no manual chown.
  Unset, the container runs as **root, exactly like v1**, so the upgrade stays
  drop-in (running non-root is recommended, not required). The real privilege is
  the Docker API it talks to, not its in-container uid. (Direct socket mount +
  non-root: use Docker's `user:` + `group_add` — the privilege drop resets
  supplementary groups; the socket-proxy path needs nothing.)
- **Site entries can no longer be turned into command-line options.** A
  `sites.conf` / `SITES` entry like `--directory-prefix=/etc` was appended bare to
  `wget`/`getent`/`ping`; GNU wget would parse it as a flag (and could write files
  inside a probed container). Exec arg-lists now place a `--` end-of-options guard
  before the URL/host, and parsing drops leading-dash / hostless entries with a
  startup warning.
- **Numeric config dials are range-validated.** Parseable-but-nonsensical values
  are now fatal rather than silently creating bugs: `TIMEOUT=0` (infinite wget),
  `WGET_TRIES=0`, `CHECK_INTERVAL=0` (busy loop), `FAIL_THRESHOLD=0`,
  `ADVISORY_DOMINANCE>1` (never fires), `DEPENDENT_VIABILITY_SAMPLES=0`, etc.
  Documented sentinels stay valid (`STATS_RETENTION_DAYS=0` = keep forever,
  `LOG_MAX_BYTES=0` = no rotation, `DEPENDENT_VIABILITY_SAMPLES=-1` = all).
- **Thread-safe site shuffle.** Dependents are probed in a thread pool; the
  load-bearing per-dependent site shuffle now draws from the RNG under a lock, so
  concurrent probes can't interleave and bias it.
- **A corrupt stats sidecar can never crash startup** — any malformed shape
  (wrong top-level/`sites`/`monitor` type, truncation, garbage) is tolerated and
  the monitor starts fresh, honoring the best-effort contract.

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
