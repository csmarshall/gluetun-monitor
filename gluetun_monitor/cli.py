"""Entry point: build config + logger + client, check prerequisites, run the loop."""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import FrameType

from .config import Config
from .docker_client import DockerClient, DockerPyClient
from .logging_setup import Logger
from .monitor import Monitor


def check_prerequisites(client: DockerClient, config: Config, logger: Logger) -> bool:
    """Verify Docker is reachable and the gluetun container exists (v1.x parity)."""
    if not Path(config.config_file).is_file():
        logger.error(f"Config file not found: {config.config_file}")
        return False
    if not client.ping():
        logger.error("Cannot connect to Docker daemon")
        return False
    if client.inspect(config.gluetun_container) is None:
        logger.error(f"Gluetun container '{config.gluetun_container}' not found")
        return False
    logger.info("Prerequisites check passed")
    return True


def _install_signal_handlers(logger: Logger) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
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
    config = Config.from_env()
    logger = Logger(log_file=config.log_file, level=config.log_level)
    _install_signal_handlers(logger)
    _announce_banner(config, logger)

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
