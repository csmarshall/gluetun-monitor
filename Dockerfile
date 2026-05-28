# gluetun-monitor v2 — Python (ADR-0007). docker-py talks the Docker API
# directly (honoring DOCKER_HOST / the socket proxy), so the docker CLI is no
# longer needed in the image.
FROM python:3.13-slim

# Create directories the monitor reads/writes.
RUN mkdir -p /app /config /logs

WORKDIR /app

# Install the package first (its own layer) for build-cache friendliness.
COPY pyproject.toml README.md LICENSE /app/
COPY gluetun_monitor /app/gluetun_monitor
RUN pip install --no-cache-dir .

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
