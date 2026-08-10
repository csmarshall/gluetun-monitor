# Docker Compose examples

## Minimal Configuration (with socket proxy)

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
    image: ghcr.io/csmarshall/gluetun-monitor:2
    # Or from Docker Hub: chasmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun  # Name of your Gluetun container
    volumes:
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

The monitor will automatically discover dependent containers and use sensible defaults. The socket proxy restricts Docker API access to only the endpoints gluetun-monitor needs.

## Full Configuration (all options)

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
    image: ghcr.io/csmarshall/gluetun-monitor:2
    # Or from Docker Hub: chasmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    depends_on:
      - docker-socket-proxy
    environment:
      - TZ=UTC
      - PUID=1000                      # run non-root (recommended); unset = root (drop-in)
      - PGID=1000
      - DOCKER_HOST=tcp://docker-socket-proxy:2375
      - GLUETUN_CONTAINER=gluetun
      - DEPENDENT_CONTAINERS=auto      # auto-discovery (default)
      - CHECK_INTERVAL=30              # seconds between checks
      - TIMEOUT=10                     # seconds per site test
      - FAIL_THRESHOLD=2               # consecutive site failures to restart gluetun
      - HEALTHY_WAIT_TIMEOUT=120       # seconds to wait for healthy status
      # --- v2 dependent-aware knobs (all optional) ---
      - DEPENDENT_CONTAINER_FAILURES=2 # consecutive per-dependent failures to remediate (default = FAIL_THRESHOLD)
      - MAX_PARALLEL_CHECKS=6          # cap on concurrent dependent probes
      - AUTO_RECREATE=1                # recreate a dependent stranded by a gluetun recreate (0 to disable)
      - DNS_WAIT_TIMEOUT=30            # seconds to wait for gluetun DNS after restart
      - LOG_LEVEL=INFO                 # DEBUG for per-site/per-dependent detail
    volumes:
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
      - ./logs:/logs
    networks:
      - docker-proxy

networks:
  docker-proxy:
    driver: bridge
```

## Alternative: Direct Socket Mount

If you prefer a simpler setup without the socket proxy, you can mount the Docker socket directly:

```yaml
services:
  gluetun-monitor:
    image: ghcr.io/csmarshall/gluetun-monitor:2
    container_name: gluetun-monitor
    restart: unless-stopped
    network_mode: none
    environment:
      - GLUETUN_CONTAINER=gluetun
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./config:/config:ro   # directory mount (put sites.conf in ./config/) — see Quick Start
      - ./logs:/logs
```

Note: This gives the container full read access to the Docker API. The socket proxy approach above is recommended for production use.
