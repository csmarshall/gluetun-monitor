# ADR-0002: Docker socket proxy is the default access method (secure-by-default)

- **Status:** Accepted
- **Date:** 2026-05-28 *(documented retroactively; decision dates to issue #10, commit 12e1bb8)*

## Context
The monitor has to control containers — inspect them, restart them, exec into
them (ADR-0001). That requires access to the Docker API. The obvious way,
bind-mounting `/var/run/docker.sock` into the container, hands a long-running,
network-adjacent watchdog **full root-equivalent control of the host**. That is
an outsized blast radius for a process whose only job is to poke a VPN container.

## Decision
Ship with a **Docker socket proxy** (`tecnativa/docker-socket-proxy`) as the
documented default. The monitor reaches it over `DOCKER_HOST=tcp://...:2375`,
and the proxy is configured to expose **only** the endpoints the monitor needs
(`CONTAINERS`, `POST`, `EXEC`). Bind-mounting the raw socket remains supported
as an explicit, opt-in fallback for users who prefer it.

## Consequences
- Least privilege by default: a compromised monitor cannot perform arbitrary
  host operations, only the narrow set the proxy permits.
- The proxy blocks `/info` by default, so prerequisite/connectivity checks use
  `docker ps` rather than `docker info` (`check_prerequisites`,
  gluetun-monitor.sh:471).
- The required permission surface **grows with the recovery strategy**: EXEC is
  needed for in-namespace testing (ADR-0001), and a recreate-based recovery
  (ADR-0004) would additionally require container create/remove. Each capability
  added to the recovery path is a capability that must be opened on the proxy —
  a tradeoff to weigh explicitly, per Tenet 3.
- The `docker:*-cli` image honors `DOCKER_HOST` natively, so supporting the
  proxy needed no changes to the script itself.
