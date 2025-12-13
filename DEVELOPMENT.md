# Development Notes

This document provides context for future development and AI assistance.

## Problem Statement

When using Gluetun as a VPN gateway for other containers (via `network_mode: container:gluetun`), there are several operational challenges:

1. **VPN endpoint failures** - Some VPN servers may become slow, blocked, or unresponsive
2. **Dependent container restarts** - When Gluetun restarts, containers using its network lose connectivity and often need to be restarted
3. **Manual intervention** - Without monitoring, users must manually detect issues and restart services
4. **Lack of visibility** - No logging of which VPN endpoints work or fail

## Solution Design

### Architecture

```
┌─────────────────────┐
│   gluetun-monitor   │
│   (this container)  │
└──────────┬──────────┘
           │ Docker Socket
           ▼
┌─────────────────────┐     ┌─────────────────────┐
│      Gluetun        │◄────│  Dependent Containers│
│   (VPN gateway)     │     │  (network_mode:      │
└─────────────────────┘     │   container:gluetun) │
                            └─────────────────────┘
```

### Key Design Decisions

#### 1. Parallel Site Testing
- **Problem:** Sequential testing takes `num_sites × timeout` seconds
- **Solution:** Launch all site tests as background jobs, wait for all to complete
- **Implementation:** Uses bash background jobs (`&`) and `wait`
- **Result:** Total test time = single timeout, regardless of site count

#### 2. Auto-Discovery of Dependent Containers
- **Problem:** Hard-coding container names is brittle and requires manual updates
- **Solution:** Query Docker API to find containers with matching `NetworkMode`
- **Implementation:** `docker inspect --format='{{.HostConfig.NetworkMode}}'`
- **Timing:** Discovery runs at startup (for logging) and immediately before each restart operation

#### 3. Consecutive Failure Threshold
- **Problem:** Transient network blips could cause unnecessary restarts
- **Solution:** Require N consecutive failures before triggering restart
- **Implementation:** Per-site failure counters, reset on success or after restart

#### 4. Memory-Safe Site Testing
- **Problem:** Large HTTP responses could consume memory
- **Solution:** Use `wget --spider` (headers only) with output discarded
- **Implementation:** `wget --spider ... >/dev/null 2>&1`

#### 5. Smart Failure Detection
- **Problem:** HTTP 4xx/5xx errors don't indicate VPN failure - the site responded
- **Solution:** Only treat actual connectivity failures as failures
- **Implementation:** Check wget exit codes:
  - Exit 0, 6, 8 → PASS (site responded, VPN working)
  - Exit 4, 5, others → FAIL (connectivity issue)
- **Rationale:** A 403 Forbidden means the VPN tunnel works; DNS resolved and TCP connected

#### 6. DNS Stabilization Wait
- **Problem:** Gluetun may report "healthy" before DNS is fully operational
- **Solution:** After health check passes, verify DNS actually works
- **Implementation:** `nslookup google.com` + `wget https://1.1.1.1` before proceeding
- **Timeout:** 30 seconds with 2-second polling

#### 7. VPN Endpoint Logging
- **Problem:** Need visibility into which endpoints fail/succeed
- **Solution:** Parse Gluetun logs for server, country, city, IP information
- **Implementation:** Grep patterns for known Gluetun log formats

## Code Structure

```
gluetun-monitor.sh
├── Configuration variables (lines 8-18)
├── log() - Timestamped logging (to stderr and log file)
├── log_endpoint_info() - Extract VPN details from Gluetun logs
├── get_gluetun_health() - Query container health status
├── discover_dependent_containers() - Find containers using Gluetun's network
├── get_dependent_containers() - Wrapper for auto/manual mode
├── wait_for_gluetun_healthy() - Poll until healthy or timeout
├── wait_for_dns_ready() - Verify DNS works after restart
├── decode_wget_exit_code() - Human-readable wget error messages
├── test_site_async() - Background job for single site test
├── test_all_sites() - Parallel test orchestration
├── restart_gluetun() - Restart with logging
├── restart_dependent_containers() - Discover and restart dependents
├── handle_failure() - Recovery orchestration with connectivity verification
├── check_prerequisites() - Startup validation
└── main() - Main loop
```

## Testing Considerations

### Manual Testing

```bash
# Test site connectivity through Gluetun
docker exec gluetun wget --spider --timeout=10 https://google.com

# Check container network mode
docker inspect --format='{{.HostConfig.NetworkMode}}' <container>

# View Gluetun logs for endpoint info
docker logs gluetun 2>&1 | grep -E "server|country|city|public IP"
```

### Simulating Failures

```bash
# Block outbound traffic in Gluetun (requires exec into container)
docker exec gluetun iptables -A OUTPUT -j DROP

# Or restart Gluetun to force new endpoint
docker restart gluetun
```

## Potential Enhancements

### Not Yet Implemented

1. **Metrics/Prometheus endpoint** - Export check results as metrics
2. **Webhook notifications** - Alert on failures/recoveries
3. **Endpoint blocklist** - Remember and avoid problematic endpoints
4. **Graceful dependent restart** - SIGTERM with timeout before SIGKILL
5. **Health endpoint** - HTTP endpoint for external monitoring
6. **Configuration hot-reload** - Reload sites.conf without restart

### Considered and Rejected

1. **Using curl instead of wget** - wget is available in Gluetun's Alpine image
2. **Running tests from monitor container** - Need to test through VPN, requires exec into Gluetun
3. **Docker Compose integration** - Would limit to single-compose deployments

## Compatibility Notes

- **Gluetun versions:** Tested with qmcgaw/gluetun, log parsing patterns may need adjustment for other versions
- **Docker API:** Uses Docker CLI, should work with any Docker version supporting `docker inspect`
- **Shell:** Requires bash (not sh) for associative arrays and other bashisms

## Log Levels

| Level | Usage |
|-------|-------|
| `INFO` | Normal operations (startup, restarts, discoveries) |
| `WARN` | Non-critical issues (single failure, unhealthy status, threshold reached) |
| `ERROR` | Critical issues (threshold exceeded, restart failed) |
| `DEBUG` | Detailed info (individual site results, timing, health checks) |
| `CHECK` | Check cycle start/end markers |
| `ENDPOINT` | VPN endpoint information (startup, failing, new) |

## Contributing

When modifying this project:

1. Maintain bash compatibility (no external dependencies beyond docker CLI)
2. Keep memory usage minimal (no storing response bodies)
3. Preserve parallel testing behavior
4. Update README.md for any new configuration options
5. Test with multiple VPN providers if possible
