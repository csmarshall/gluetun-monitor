"""Entry point: build config + logger + client, check prerequisites, run the loop."""

from __future__ import annotations

import signal
import sys
from types import FrameType

from .config import Config
from .docker_client import DockerClient, DockerPyClient
from .logging_setup import Logger
from .monitor import Monitor
from .sites import load_sites


def check_prerequisites(client: DockerClient, config: Config, logger: Logger) -> bool:
    """Validate everything required to start. Any failure is fatal (return False).

    We refuse to start on bad config rather than guess, because guessing can lead
    to acting on the wrong containers (Tenet 2/6). Specifically: there must be at
    least one testable site (from sites.conf and/or the SITES env — an empty set
    is a fake-green trap), the Docker API must be reachable, gluetun must exist,
    and an explicit DEPENDENT_CONTAINERS list must name only containers that exist.
    """
    if not load_sites(config.config_file, config.sites_env):
        logger.error(
            "No testable sites configured: provide URLs via the sites file "
            f"({config.config_file}) and/or the SITES env var — refusing to run a "
            f"monitor that tests nothing"
        )
        return False
    if not client.ping():
        logger.error("Cannot connect to Docker daemon")
        return False
    if client.inspect(config.gluetun_container) is None:
        logger.error(f"Gluetun container '{config.gluetun_container}' not found")
        return False
    if config.dependent_containers != "auto":
        names = [n for n in (s.strip() for s in config.dependent_containers.split(",")) if n]
        if not names:
            logger.error(
                "DEPENDENT_CONTAINERS is set but lists no valid container names; "
                "use 'auto' for discovery or name existing containers"
            )
            return False
        missing = [n for n in names if client.inspect(n) is None]
        if missing:
            logger.error(
                f"DEPENDENT_CONTAINERS names container(s) not found: {', '.join(missing)} "
                f"— refusing to start (will not guess which containers to manage)"
            )
            return False
    logger.info("Prerequisites check passed")
    return True


def _install_signal_handlers(logger: Logger) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        """Log the signal and exit cleanly (0) so the container stops gracefully."""
        logger.info(f"Received signal {signal.Signals(signum).name}, exiting...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _announce_banner(config: Config, logger: Logger) -> None:
    """The startup banner, logged before prerequisites (v1.x order) so it is
    visible even when the prereq check then fails."""
    logger.info("Gluetun Monitor starting...")
    logger.info(
        f"Config: CHECK_INTERVAL={config.check_interval}s, TIMEOUT={config.timeout}s, "
        f"FAIL_THRESHOLD={config.fail_threshold}, "
        f"DEPENDENT_CONTAINER_FAILURES={config.dependent_container_failures}, "
        f"AUTO_RECREATE={int(config.auto_recreate)}"
    )
    logger.info(f"Monitoring container: {config.gluetun_container}")


def main() -> int:
    """Build config + logger + Docker client, check prerequisites, run the loop.

    Returns a process exit code: 0 on clean shutdown, 1 if startup fails.
    """
    config = Config.from_env()
    logger = Logger(log_file=config.log_file, level=config.log_level)
    _install_signal_handlers(logger)
    _announce_banner(config, logger)

    if config.errors:  # malformed env values — fatal; don't run with unparseable params
        for err in config.errors:
            logger.error(err)
        logger.error("Refusing to start due to invalid configuration")
        return 1

    try:
        client: DockerClient = DockerPyClient(timeout=max(config.timeout * 2, 60))
    except Exception as exc:
        logger.error(f"Failed to initialize Docker client: {exc}")
        return 1

    if not check_prerequisites(client, config, logger):
        return 1

    monitor = Monitor(client, config, logger)
    monitor.announce()
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
