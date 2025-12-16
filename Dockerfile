FROM docker:29-cli

# Install bash and other utilities
RUN apk add --no-cache bash coreutils grep tzdata

# Create directories
RUN mkdir -p /app /config /logs

# Copy the monitor script
COPY gluetun-monitor.sh /app/gluetun-monitor.sh
RUN chmod +x /app/gluetun-monitor.sh

# Default environment variables
ENV CONFIG_FILE=/config/sites.conf
ENV LOG_FILE=/logs/gluetun-monitor.log
ENV CHECK_INTERVAL=30
ENV TIMEOUT=10
ENV FAIL_THRESHOLD=2
ENV GLUETUN_CONTAINER=gluetun
ENV DEPENDENT_CONTAINERS=auto
ENV HEALTHY_WAIT_TIMEOUT=120
ENV DISCOVERY_INTERVAL=300

WORKDIR /app

CMD ["/app/gluetun-monitor.sh"]
