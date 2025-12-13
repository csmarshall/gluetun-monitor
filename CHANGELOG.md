# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
