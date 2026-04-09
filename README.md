# Gluetun Monitor

<p align="center">
  <img src="logo.svg" alt="Gluetun Monitor Logo" width="150" height="150">
</p>

<p align="center">
  <a href="https://github.com/csmarshall/gluetun-monitor/actions/workflows/ci.yml"><img src="https://github.com/csmarshall/gluetun-monitor/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/csmarshall/gluetun-monitor/releases"><img src="https://img.shields.io/github/v/release/csmarshall/gluetun-monitor" alt="GitHub release"></a>
  <a href="https://hub.docker.com/r/chasmarshall/gluetun-monitor"><img src="https://img.shields.io/docker/pulls/chasmarshall/gluetun-monitor" alt="Docker Pulls"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

A lightweight Docker container that monitors VPN connectivity through [Gluetun](https://github.com/qdm12/gluetun) and automatically recovers from connection failures by restarting Gluetun and its dependent containers.

## Links

- **GitHub Repository**: https://github.com/csmarshall/gluetun-monitor
- **Docker Hub**: https://hub.docker.com/r/chasmarshall/gluetun-monitor
- **GitHub Container Registry**: https://github.com/csmarshall/gluetun-monitor/pkgs/container/gluetun-monitor
- **Releases**: https://github.com/csmarshall/gluetun-monitor/releases

**Pull the image:**
```bash
# From Docker Hub
docker pull chasmarshall/gluetun-monitor:latest

# From GitHub Container Registry
docker pull ghcr.io/csmarshall/gluetun-monitor:latest
```

## Features

- **Multi-site health checking** - Tests connectivity to multiple endpoints simultaneously
- **Parallel testing** - All sites tested concurrently for fast detection (bounded by single timeout)
- **Auto-discovery** - Automatically finds containers using Gluetun's network
- **Automatic recovery** - Restarts Gluetun and dependent containers on failure
- **Endpoint logging** - Logs VPN server details (server, country, city, IP) on failures and recoveries
- **Change detection** - Logs when dependent containers are added or removed
- **Configurable thresholds** - Consecutive failure count before triggering restart
- **Low resource usage** - Uses `wget --spider` (headers only, no body download)

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                       Gluetun Monitor                           │
├─────────────────────────────────────────────────────────────────┤
│  1. Test sites through Gluetun's network (parallel)             │
│  2. If failures exceed threshold:                               │
│     a. Log current VPN endpoint info                            │
│     b. Restart Gluetun                                          │
│     c. Wait for Gluetun to become healthy                       │
│     d. Log new VPN endpoint info                                │
│     e. Auto-discover and restart dependent containers           │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Pull the image

```bash
docker pull ghcr.io/csmarshall/gluetun-monitor:latest
```

### 2. Copy example configs

```bash
# If cloning the repo:
cp docker-compose.yml.example docker-compose.yml
cp sites.conf.example sites.conf
```

### 3. Configure

Edit `docker-compose.yml`:
- Set `GLUETUN_CONTAINER` to your gluetun container name
- Adjust `TZ` for your timezone

Edit `sites.conf` with endpoints to test:

```conf
# Sites to test for VPN connectivity
https://www.google.com
https://cloudflare.com
https://1.1.1.1
# Add sites you need to reach through VPN
```

### 4. Deploy

```bash
docker compose up -d
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DOCKER_HOST` | *(unset)* | Docker daemon endpoint. Set to `tcp://docker-socket-proxy:2375` when using a socket proxy |
| `GLUETUN_CONTAINER` | `gluetun` | Name of the Gluetun container to monitor |
| `DEPENDENT_CONTAINERS` | `auto` | `auto` to discover dynamically, or comma-separated list |
| `CHECK_INTERVAL` | `30` | Seconds between health checks |
| `TIMEOUT` | `10` | Seconds to wait for each site test |
| `FAIL_THRESHOLD` | `2` | Consecutive failures before triggering restart |
| `HEALTHY_WAIT_TIMEOUT` | `120` | Max seconds to wait for Gluetun to become healthy after restart |
| `TZ` | `UTC` | Timezone for log timestamps |

### Variable Details

#### `DOCKER_HOST`
When unset, the Docker CLI connects via the local socket (`/var/run/docker.sock`). Set this to `tcp://<proxy-host>:2375` to connect through a [Docker socket proxy](#docker-socket-proxy) instead of mounting the socket directly. See the [Docker Socket Proxy](#docker-socket-proxy) section for setup details.

#### `GLUETUN_CONTAINER`
The name of your Gluetun container as shown in `docker ps`. This is the container that will be:
- Used to execute site connectivity tests (via `docker exec`)
- Monitored for health status
- Restarted when connectivity fails
- Used to extract VPN endpoint information from logs

#### `DEPENDENT_CONTAINERS`
Controls which containers are restarted after Gluetun recovers:
- `auto` - Automatically discovers containers using `network_mode: "container:<GLUETUN_CONTAINER>"`
- `container1,container2` - Comma-separated list of container names to restart

Auto-discovery queries the Docker API to inspect each running container's `NetworkMode` setting.

#### `CHECK_INTERVAL`
Time in seconds between health check cycles. Each cycle tests all configured sites in parallel.

**Note:** The actual interval is `CHECK_INTERVAL` + up to `TIMEOUT` seconds (for parallel site tests).

#### `TIMEOUT`
Maximum seconds to wait for each site to respond. Since tests run in parallel, this is the maximum time for the entire test batch, not per-site.

Uses `wget --spider` which only fetches headers (no response body downloaded).

### Site Test Success/Failure Logic

The monitor distinguishes between **connectivity failures** (VPN broken) and **site errors** (VPN working, site returned an error):

| wget Exit Code | Meaning | Treated As | Rationale |
|----------------|---------|------------|-----------|
| 0 | Success (HTTP 2xx/3xx) | **PASS** | Site responded successfully |
| 6 | Authentication required | **PASS** | Site responded (VPN working) |
| 8 | Server error (HTTP 4xx/5xx) | **PASS** | Site responded (VPN working) |
| 4 | Network failure | **FAIL** | DNS or connection failed |
| 5 | SSL verification failure | **FAIL** | Possible MITM or connectivity issue |
| 1-3, 7 | Other errors | **FAIL** | Various connectivity issues |

**Key insight:** If a site returns HTTP 403 Forbidden or 503 Service Unavailable, the VPN is working - the site just doesn't like the request. Only actual network/DNS failures indicate a VPN problem.

#### `FAIL_THRESHOLD`
Number of **consecutive** failures for a site before triggering a restart. This prevents restarts from transient network blips.

Example with `FAIL_THRESHOLD=2`:
- Check 1: Site fails → Counter: 1 (no action)
- Check 2: Site fails → Counter: 2 (triggers restart)
- After restart: Counter reset to 0

#### `HEALTHY_WAIT_TIMEOUT`
Maximum seconds to wait for Gluetun to report "healthy" status after a restart. Gluetun must have a healthcheck configured for this to work.

If Gluetun doesn't become healthy within this timeout, the monitor logs an error but continues operating.

### Dependent Container Discovery

By default (`DEPENDENT_CONTAINERS=auto`), the monitor automatically finds all containers that depend on Gluetun by querying the Docker API for containers with:

```
network_mode: "container:<GLUETUN_CONTAINER>"
```

This just works out of the box - no configuration needed. Discovery runs at startup (for logging) and again before each restart operation to ensure newly added containers are included.

**Note:** Containers added after startup will be discovered and restarted when the next failure triggers a recovery. There's no continuous polling for new containers during normal operation.

#### Advanced: Manual Override

In rare cases where you need explicit control (e.g., restart only specific containers, or include containers that don't use network_mode), you can specify a manual list:

```yaml
environment:
  - DEPENDENT_CONTAINERS=container1,container2,container3
```

## Docker Compose Example

### Minimal Configuration (with socket proxy)

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: gluetun-monitor-socket-proxy
    restart: unless-stopped
    environment:
      - CONTAINERS=1
      - POST=1
      - EXEC=1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker-proxy

  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:latest
    # Or from Docker Hub: chasmarshall/gluetun-monitor:latest
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun  # Name of your Gluetun container
    volumes:
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

The monitor will automatically discover dependent containers and use sensible defaults. The socket proxy restricts Docker API access to only the endpoints gluetun-monitor needs.

### Full Configuration (all options)

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: gluetun-monitor-socket-proxy
    restart: unless-stopped
    environment:
      - CONTAINERS=1
      - POST=1
      - EXEC=1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker-proxy

  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:latest
    # Or from Docker Hub: chasmarshall/gluetun-monitor:latest
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - TZ=UTC
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun
      - DEPENDENT_CONTAINERS=auto      # auto-discovery (default)
      - CHECK_INTERVAL=30              # seconds between checks
      - TIMEOUT=10                     # seconds per site test
      - FAIL_THRESHOLD=2               # consecutive failures to trigger restart
      - HEALTHY_WAIT_TIMEOUT=120       # seconds to wait for healthy status
    volumes:
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

### Alternative: Direct Socket Mount

If you prefer a simpler setup without the socket proxy, you can mount the Docker socket directly:

```yaml
services:
  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:latest
    container_name: gluetun-monitor
    restart: unless-stopped
    network_mode: none
    environment:
      - GLUETUN_CONTAINER=gluetun
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./sites.conf:/config/sites.conf:ro
      - ./logs:/logs
```

Note: This gives the container full read access to the Docker API. The socket proxy approach above is recommended for production use.

## Log Output

### Startup
```
[2025-01-15 10:00:00] [INFO] Gluetun Monitor starting...
[2025-01-15 10:00:00] [INFO] Config: CHECK_INTERVAL=30s, TIMEOUT=10s, FAIL_THRESHOLD=2
[2025-01-15 10:00:00] [INFO] Monitoring container: gluetun
[2025-01-15 10:00:00] [INFO] Prerequisites check passed
[2025-01-15 10:00:00] [INFO] Docker connection: socket proxy (tcp://docker-socket-proxy:2375)
[2025-01-15 10:00:00] [INFO] Dependent containers (auto-discovery): app1,app2,app3
[2025-01-15 10:00:00] [ENDPOINT] Status: STARTUP | IP: 203.x.x.x | Country: United States | City: New York | VPN Server: us123.vpn.com | Reason: Monitor starting
```

### Failure and Recovery
```
[2025-01-15 10:10:00] [WARN] Site https://example.com failed 2 consecutive times - THRESHOLD REACHED - Network failure (DNS or connection)
[2025-01-15 10:10:00] [ERROR] Failed sites (exceeded threshold): https://example.com
[2025-01-15 10:10:00] [WARN] Health check failed, initiating recovery...
[2025-01-15 10:10:00] [ENDPOINT] Status: FAILING | IP: 203.x.x.x | Country: United States | City: New York | VPN Server: us123.vpn.com | Reason: Site connectivity test failed
[2025-01-15 10:10:05] [INFO] Restarting gluetun-nordvpn-wg to force new endpoint...
[2025-01-15 10:10:35] [INFO] gluetun-nordvpn-wg is healthy after 30s
[2025-01-15 10:10:40] [ENDPOINT] Status: NEW | IP: 89.x.x.x | Country: Germany | City: Frankfurt | VPN Server: de456.vpn.com | Reason: After restart
[2025-01-15 10:10:40] [INFO] Discovering and restarting dependent containers...
[2025-01-15 10:10:41] [INFO] Discovered dependent containers: app1,app2
[2025-01-15 10:10:42] [INFO] Restarting app1...
[2025-01-15 10:10:44] [INFO] app1 restarted successfully
[2025-01-15 10:10:46] [INFO] Restarting app2...
[2025-01-15 10:10:48] [INFO] app2 restarted successfully
[2025-01-15 10:10:48] [INFO] Dependent container restart complete
[2025-01-15 10:10:48] [INFO] Recovery complete
```

## Requirements

- Docker with API access (via [socket proxy](#docker-socket-proxy) or direct socket mount)
- Gluetun container with a healthcheck configured
- Dependent containers using `network_mode: "container:<gluetun>"`

## How Gluetun Network Mode Works

Containers can share Gluetun's network stack using Docker's container network mode:

```yaml
# Gluetun container
services:
  gluetun:
    image: qmcgaw/gluetun
    container_name: gluetun
    ports:
      - 8080:8080  # Expose ports for dependent apps here
    # ... VPN configuration

  # App using Gluetun's network
  myapp:
    image: myapp:latest
    network_mode: "container:gluetun"
    depends_on:
      - gluetun
    # Note: ports must be defined on gluetun, not here
```

When Gluetun restarts, containers using its network lose connectivity and typically need to be restarted. This monitor automates that process.

## How Auto-Discovery Works

The monitor automatically discovers dependent containers by communicating with the Docker daemon — either through a [socket proxy](#docker-socket-proxy) (recommended) or a direct [Docker socket](https://docs.docker.com/engine/reference/commandline/dockerd/#daemon-socket-option) mount.

### Docker API Access

The monitor needs access to the Docker API to list, inspect, restart, and exec into containers. There are two ways to provide this:

**Socket proxy (recommended):** Set `DOCKER_HOST=tcp://socket-proxy:2375` and connect through an isolated network. The proxy limits API access to only the endpoints needed. See [Docker Socket Proxy](#docker-socket-proxy).

**Direct socket mount:** Mount `/var/run/docker.sock` into the container. Simpler but grants full Docker API access.

```yaml
# Socket proxy (recommended)
environment:
  - DOCKER_HOST=tcp://docker-socket-proxy:2375

# Or direct mount (simpler, less secure)
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

This is the same mechanism used by tools like [Portainer](https://www.portainer.io/), [Traefik](https://traefik.io/), and [Watchtower](https://containrrr.dev/watchtower/).

### Discovery Process

When discovery runs, the monitor:

1. **Queries Docker** for all running container IDs via `docker ps -q`
2. **Inspects each container** to get its `NetworkMode` setting via `docker inspect`
3. **Matches containers** where `NetworkMode` equals `container:<gluetun-name>` or `container:<gluetun-id>`
4. **Returns the list** of dependent container names

```bash
# What the monitor does internally:
docker inspect --format='{{.HostConfig.NetworkMode}}' <container_id>
# Returns: "container:gluetun" or "container:abc123def456..."
```

### When Discovery Runs

- **At startup** - For logging which containers will be managed
- **Before each restart** - To catch any containers added since startup

This approach means no configuration is needed - containers are discovered dynamically based on their actual Docker configuration.

For more details on the Docker Engine API, see the [official documentation](https://docs.docker.com/engine/api/).

## Docker Socket Proxy

The recommended deployment uses a [Docker socket proxy](https://github.com/Tecnativa/docker-socket-proxy) to restrict which Docker API endpoints gluetun-monitor can access. Instead of mounting the Docker socket directly (which grants full API access), the proxy exposes only the specific capabilities needed:

| Proxy Setting | Required For |
|---------------|-------------|
| `CONTAINERS=1` | Listing, inspecting, and reading logs of containers |
| `POST=1` | Restarting containers (also enables other POST operations like stop/kill on any container) |
| `EXEC=1` | Running connectivity tests inside the Gluetun container |

The proxy and gluetun-monitor communicate over an isolated Docker network (`docker-proxy`). Only the proxy container has access to the Docker socket.

## Security Considerations

- **Socket proxy (recommended):** Limits Docker API access to container operations (list, inspect, logs, restart, stop, exec). The monitor cannot pull images, build images, access volumes, manage networks, or perform other Docker operations. Note that `POST=1` enables all POST actions on allowed endpoints, not just restart.
- **Direct socket mount:** The Docker socket is mounted read-only (`:ro`), but socket operations (restart, exec) still function. The monitor can interact with any container — ensure your Docker environment is trusted.
- No credentials or sensitive data are logged
- Site test responses are discarded (headers only, no body)

## Building

```bash
docker compose build
```

Or manually:

```bash
docker build -t gluetun-monitor .
```

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions welcome! Please open an issue or pull request.

## Acknowledgments

- [Gluetun](https://github.com/qdm12/gluetun) - The excellent VPN client container this monitor is designed for
