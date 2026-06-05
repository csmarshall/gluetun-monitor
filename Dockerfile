# gluetun-monitor v2 — Python (ADR-0007). docker-py talks the Docker API
# directly (honoring DOCKER_HOST / the socket proxy), so the docker CLI is no
# longer needed in the image.
FROM python:3.14-slim

# gosu lets the entrypoint drop privileges to PUID:PGID when the operator opts in
# (LSIO-style). With no PUID/PGID the container runs as root — a drop-in match for
# v1, with no host-side chown. Running non-root is recommended; see the README.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /config /logs

WORKDIR /app

# Runtime deps come from a fully-pinned, hashed lock (ADR-0013): a deterministic,
# integrity-checked dependency tree and a stable Python layer for the base-image
# drift check. Installed before the source so a code change doesn't re-resolve deps.
# Hashes in the file auto-enable pip's --require-hashes. The lock is kept in sync
# with pyproject by a CI guard.
COPY requirements.lock /app/
RUN pip install --no-cache-dir -r requirements.lock

# Then the package itself, without deps (already installed, pinned, from the lock).
COPY pyproject.toml README.md LICENSE /app/
COPY gluetun_monitor /app/gluetun_monitor
RUN pip install --no-cache-dir --no-deps .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

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

# The entrypoint optionally drops to PUID:PGID, then runs the console script.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gluetun-monitor"]
