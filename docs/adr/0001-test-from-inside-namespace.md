# ADR-0001: Test connectivity from inside gluetun's network namespace

- **Status:** Accepted *(extended by ADR-0006)*
- **Date:** 2026-05-28 *(documented retroactively; decision dates to v1.0.0)*

## Context
The entire job of this tool is to detect when the VPN tunnel has stopped
carrying traffic. A connectivity check run from the host — or from the monitor's
own container — egresses via normal host routing, not through gluetun's tunnel.
Such a check can pass while the tunnel is dead, or fail for reasons that have
nothing to do with the VPN. The only signal that actually means anything is
whether traffic *that is supposed to traverse the tunnel* reaches the internet.

## Decision
All connectivity tests run **inside gluetun's network namespace** via
`docker exec "$GLUETUN_CONTAINER" wget ...` (`test_site_async`,
gluetun-monitor.sh:223). The monitor never curls/wgets from the host. Sites are
tested in parallel (`test_all_sites`), and the reported public IP / country /
VPN server are parsed from **gluetun's own logs** (`log_endpoint_info`,
gluetun-monitor.sh:47) for the same reason — they reflect the tunnel's real
egress, not the host's.

## Consequences
- A pass genuinely means egress through the VPN works, and the IP we log is the
  tunnel's public IP.
- This requires `docker exec` over the Docker connection, so the socket proxy
  must allow the EXEC endpoint (see ADR-0002), and the target container must ship
  a usable `wget` (the gluetun image does).
- This is the load-bearing premise of the whole tool. "Simplifying" it to a
  host-side check would silently defeat its purpose — a fact worth recording so
  nobody undoes it later.
- It also bounds what the monitor can see on its own: it observes only what
  *gluetun itself* can reach, **not** the independent connectivity of the
  containers that route through gluetun. That blind spot is closed by ADR-0004
  (per-dependent strand detection) and ADR-0006 (per-dependent connectivity + DNS
  testing).
