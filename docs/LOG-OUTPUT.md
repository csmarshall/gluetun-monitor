# Log output

## Startup
```
[2025-01-15 10:00:00] [INFO] Gluetun Monitor starting...
[2025-01-15 10:00:00] [INFO] Config: CHECK_INTERVAL=30s, TIMEOUT=10s, FAIL_THRESHOLD=2, DEPENDENT_CONTAINER_FAILURES=2, AUTO_RECREATE=1
[2025-01-15 10:00:00] [INFO] Monitoring container: gluetun
[2025-01-15 10:00:00] [INFO] Prerequisites check passed
[2025-01-15 10:00:00] [INFO] Docker connection: socket proxy (tcp://docker-socket-proxy:2375)
[2025-01-15 10:00:00] [INFO] Dependent containers (auto-discovery): app1,app2,app3
[2025-01-15 10:00:00] [ENDPOINT] Status: STARTUP | IP: 203.x.x.x | Country: United States | City: New York | VPN Server: us123.vpn.com | Reason: Monitor starting
```

## A normal check cycle (DEBUG)
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

## Gluetun connectivity failure + recovery
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

## Dependent stranded by a Gluetun recreate (self-healed)
```
[2025-01-15 11:00:00] [WARN] Remediating dependent qbittorrent: stranded loopback-only
[2025-01-15 11:00:00] [WARN] qbittorrent netns moved (gluetun recreated) → recreate
[2025-01-15 11:00:00] [WARN] Recreating qbittorrent (re-homing netns onto gluetun 9f3c1a2b4d5e)
[2025-01-15 11:00:02] [INFO] qbittorrent recreated as 7a1b2c3d4e5f and started
[2025-01-15 11:00:04] [INFO] qbittorrent verified healthy after remediation
```

## Log rotation (both sinks)

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
