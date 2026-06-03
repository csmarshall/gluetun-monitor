"""Entry point: build config + logger + client, check prerequisites, run the loop."""

from __future__ import annotations

import signal
import sys
from types import FrameType

from .config import Config
from .dependents import parse_csv_names
from .docker_client import DockerClient, DockerPyClient
from .logging_setup import Logger, install_bash_format_on_root
from .monitor import Monitor
from .sites import load_sites_report


def check_prerequisites(client: DockerClient, config: Config, logger: Logger) -> bool:
    """Validate everything required to start. Any failure is fatal (return False).

    We refuse to start on bad config rather than guess, because guessing can lead
    to acting on the wrong containers (Tenet 3/7). Specifically: there must be at
    least one testable site (from sites.conf and/or the SITES env — an empty set
    is a fake-green trap), the Docker API must be reachable, gluetun must exist,
    and an explicit DEPENDENT_CONTAINERS list must name only containers that exist
    (excluding any in EXCLUDE_CONTAINERS — those need not exist). EXCLUDE issues
    (overlap with the include list, or names that match nothing) WARN but are not
    fatal: excluding is the "do no harm" direction.
    """
    try:
        sites, rejected = load_sites_report(config.config_file, config.sites_env)
    except OSError as exc:
        # e.g. CONFIG_FILE is a directory (a missing bind-mount source that Docker
        # silently created) or unreadable — fail loud and cleanly, not a traceback.
        logger.error(f"Cannot read sites config {config.config_file}: {exc}")
        return False
    for entry, reason in rejected:
        logger.warn(f"Ignoring unsafe site entry {entry!r}: {reason}")
    if not sites:
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

    excluded = parse_csv_names(config.exclude_containers)
    if config.dependent_containers != "auto":
        names = parse_csv_names(config.dependent_containers)
        if not names:
            logger.error(
                "DEPENDENT_CONTAINERS is set but lists no valid container names; "
                "use 'auto' for discovery or name existing containers"
            )
            return False
        overlap = sorted(set(names) & set(excluded))
        if overlap:
            logger.warn(
                f"Container(s) in both DEPENDENT_CONTAINERS and EXCLUDE_CONTAINERS: "
                f"{', '.join(overlap)} — excluding them (first, do no harm)"
            )
        # Excluded names need not exist (the point is to not manage them); only the
        # effectively-included names are required to be present.
        effective = [n for n in names if n not in excluded]
        if not effective:
            logger.warn(
                "Every name in DEPENDENT_CONTAINERS is also excluded; "
                "no dependents will be managed (gluetun-only)"
            )
        else:
            missing = [n for n in effective if client.inspect(n) is None]
            if missing:
                logger.error(
                    f"DEPENDENT_CONTAINERS names container(s) not found: {', '.join(missing)} "
                    f"— refusing to start (will not guess which containers to manage)"
                )
                return False

    # An exclude name matching nothing is usually a typo — and a dangerous one,
    # since the container you meant to protect would still be managed. Warn (not
    # fatal: it may legitimately not exist yet).
    unmatched = [n for n in excluded if client.inspect(n) is None]
    if unmatched:
        logger.warn(
            f"EXCLUDE_CONTAINERS names container(s) not found: {', '.join(unmatched)} "
            f"(typo? they currently exclude nothing)"
        )
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
    visible even when the prereq check then fails.
    """
    logger.info("Gluetun Monitor starting...")
    logger.info(
        f"Config: CHECK_INTERVAL={config.check_interval}s, TIMEOUT={config.timeout}s, "
        f"FAIL_THRESHOLD={config.fail_threshold}, "
        f"DEPENDENT_CONTAINER_FAILURES={config.dependent_container_failures}, "
        f"AUTO_RECREATE={int(config.auto_recreate)}"
    )
    logger.info(f"Monitoring container: {config.gluetun_container}")
    if config.dry_run:
        logger.warn(
            "DRY_RUN enabled: observe-only — logs intended actions, never restarts/recreates"
        )


def main() -> int:
    """Build config + logger + Docker client, check prerequisites, run the loop.

    Returns a process exit code: 0 on clean shutdown, 1 if startup fails.
    """
    config = Config.from_env()
    logger = Logger(
        log_file=config.log_file,
        level=config.log_level,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    install_bash_format_on_root()  # make stray docker-py/urllib3 logs match our format
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
