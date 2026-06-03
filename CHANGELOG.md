# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.2] - 2026-06-03

### Changed
- **v1 is end-of-life.** This release adds a one-time **EOL notice** at startup
  pointing to v2, and is the final v1 build. v1 stays pullable at the frozen `:1`
  tag as a rollback anchor; it will not receive further changes.
- **v2 is a drop-in upgrade** — change the image tag to `:2` (same env vars,
  files, and socket-proxy permissions) to also get dependent-aware health +
  self-healing. See the v2 CHANGELOG/README on `main`.

### Note
- This v1.x release publishes only the `:1` / `:1.1` / `:1.1.2` tags — it does
  **not** update `:latest` (which tracks the current major, v2).

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
