# gluetun-monitor v2 — Python (ADR-0007). docker-py talks the Docker API
# directly (honoring DOCKER_HOST / the socket proxy), so the docker CLI is no
# longer needed in the image.
FROM python:3.13-slim

# Create a non-root user to run as (defense in depth — the real privilege is the
# Docker API the monitor talks to, not its in-container uid, but there's no reason
# to run as root). /config and /logs are owned by it so the read-only sites mount
# and the writable log/stats dir both work. NOTE: with the *direct* socket mount
# (the docker-compose alternative), /var/run/docker.sock is root:docker — a
# non-root process needs that group; the recommended socket-proxy path over
# DOCKER_HOST=tcp:// needs no special permission and works as-is.
RUN useradd --system --uid 10001 --create-home --home-dir /home/monitor monitor \
    && mkdir -p /app /config /logs \
    && chown -R monitor:monitor /app /config /logs

WORKDIR /app

# Install the package first (its own layer) for build-cache friendliness.
COPY pyproject.toml README.md LICENSE /app/
COPY gluetun_monitor /app/gluetun_monitor
RUN pip install --no-cache-dir .

USER monitor

# Default environment (matches the v1.x contract; v2 adds the dependent knobs).
ENV CONFIG_FILE=/config/sites.conf \
    LOG_FILE=/logs/gluetun-monitor.log \
    CHECK_INTERVAL=30 \
    TIMEOUT=10 \
    FAIL_THRESHOLD=2 \
    GLUETUN_CONTAINER=gluetun \
    DEPENDENT_CONTAINERS=auto \
    HEALTHY_WAIT_TIMEOUT=120 \
    DEPENDENT_CONTAINER_FAILURES=2 \
    MAX_PARALLEL_CHECKS=6 \
    AUTO_RECREATE=1 \
    LOG_LEVEL=INFO

# Run as the installed console script.
CMD ["gluetun-monitor"]
