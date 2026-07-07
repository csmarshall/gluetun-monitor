"""Entry point: build config + logger + client, check prerequisites, run the loop."""

from __future__ import annotations

import argparse
import signal
import sys
from types import FrameType

from .config import Config
from .dependents import parse_csv_names
from .docker_client import DockerClient, DockerPyClient
from .logging_setup import Logger, install_bash_format_on_root
from .monitor import Monitor
from .notify import Notifier, NotifyEvent, build_notifier
from .site_stats import SiteStatsStore
from .sites import hostname_of, load_sites_report, load_specs, worst_case_probe_seconds
from .tunables import suggest_tunables


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


_NOTIFY_LEVEL_BLURB = {
    "attention": "Pinged only when your attention is needed (failed recovery/remediation, "
    "refused start, flaky-site advisory). Self-heals, changes, and the firehose stay silent.",
    "recovery": "Also pinged on self-healed incidents (gluetun/dependent recovered).",
    "activity": "Also pinged on non-fault changes (sites reloaded, endpoint changed).",
    "all": "Pinged on everything, including per-loop checks (the firehose).",
}


def _log_notify_summary(config: Config, logger: Logger) -> None:
    """Tell the operator exactly what they signed up for — including what stays silent."""
    if not config.apprise_urls:
        logger.info("Notifications: disabled (log-only). Set APPRISE_URLS to enable (see README).")
        return
    # Schemes only — the full URLs carry tokens and must never be logged.
    schemes = sorted({u.split("://", 1)[0] for u in config.apprise_urls if "://" in u})
    logger.info(
        f"Notifications: ENABLED — {len(config.apprise_urls)} target(s) "
        f"[{', '.join(schemes)}], level={config.notify_level}"
    )
    logger.info(f"  {_NOTIFY_LEVEL_BLURB.get(config.notify_level, '')}")
    if config.notify_repeat_interval == 0:
        logger.info(
            "  Ongoing problems: announced once, then silent until they resolve "
            "(NOTIFY_REPEAT_INTERVAL=0)."
        )
    else:
        logger.info(
            f"  Ongoing problems: reminded every {config.notify_repeat_interval} loop(s) "
            "while unresolved (NOTIFY_REPEAT_INTERVAL)."
        )
    logger.info("  Resolve notices: on — you'll hear when a problem clears.")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gluetun-monitor",
        description="Dependent-aware connectivity monitor and self-healer for gluetun VPN stacks",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="send a test notification to APPRISE_URLS and exit (verifies your config)",
    )
    parser.add_argument(
        "--suggest-tunables",
        action="store_true",
        help="print per-URL timeout/tries suggestions from the persisted site stats "
        "and exit (paste the '|key=value' lines into sites.conf — see README)",
    )
    return parser.parse_args(argv)


def _run_suggest_tunables(config: Config, logger: Logger) -> int:
    """Print per-URL tunable suggestions derived from the persisted site stats (#60).

    A read-only report to stdout (not the rotating log): for each site that keeps
    causing avoidable restarts, the paste-ready ``url|key=value`` override the data
    supports — or, when no knob helps, the advice to review/remove it. Suggestions
    are advisory only; nothing is changed.
    """
    store = SiteStatsStore(config.stats_file)
    if not store.sites:
        logger.info(
            f"No site stats yet at {config.stats_file or '(STATS_FILE unset)'} — "
            "run the monitor a while, then retry."
        )
        return 0
    try:
        specs = {s.url: s for s in load_specs(config.config_file, config.sites_env)}
    except Exception:
        specs = {}  # unreadable sites config → judge against the globals, as before
    suggestions = suggest_tunables(
        store.sites, global_timeout=config.timeout, global_tries=config.wget_tries,
        specs=specs,  # judge each site against its EFFECTIVE knobs (#77)
    )
    print("gluetun-monitor — per-URL tunable suggestions")
    print(
        f"  source: {config.stats_file}   "
        f"globals: TIMEOUT={config.timeout}s WGET_TRIES={config.wget_tries}\n"
    )
    if not suggestions:
        print(
            f"  No suggestions — all {len(store.sites)} site(s) are behaving within "
            "the global defaults."
        )
        return 0
    flagged = set()
    for s in suggestions:
        flagged.add(s.url)
        print(f"  {hostname_of(s.url)}")
        print(f"    {s.rationale}")
        print(f"    → {s.config_line}" if s.config_line
              else "    → review / remove, or enable auto-backoff (#27)")
        print()
    healthy = sorted(hostname_of(u) for u in store.sites if u not in flagged)
    if healthy:
        print(f"  Healthy (no change): {', '.join(healthy)}")
    return 0


def _transport_timeout(config: Config) -> int:
    """Initial Docker transport read-timeout: double the worst-case probe runtime,
    floored at 60s (the pre-#77 shape, so no-override configs size identically).

    Sized from the *effective* per-URL knobs, not just the global ``TIMEOUT``: a
    ``|timeout=`` override above the global-derived ceiling would otherwise have
    its probes killed by the transport — a slow success misreported as a failure,
    by the very knob meant to stop that (#77). The monitor re-raises the ceiling
    on every sites reload (``DockerClient.ensure_timeout``); this only has to be
    right for startup.
    """
    worst = config.timeout * max(1, config.wget_tries)
    try:
        specs = load_specs(config.config_file, config.sites_env)
        worst = worst_case_probe_seconds(specs, config.timeout, config.wget_tries)
    except Exception:
        pass  # unreadable sites config is reported at loop time; size from the globals
    return max(worst * 2, 60)


def _run_notify_test(config: Config, notifier: Notifier, logger: Logger) -> int:
    """Send a one-off test notification and report the outcome."""
    if not config.apprise_urls:
        logger.error("APPRISE_URLS is not set — there is nothing to test")
        return 1
    logger.info(f"Sending a test notification to {len(config.apprise_urls)} configured URL(s)...")
    if notifier.test():
        logger.info("Test notification sent successfully")
        return 0
    logger.error("Test notification failed to send — check the Apprise URL/scheme (DEBUG logs)")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Build config + logger + Docker client, check prerequisites, run the loop.

    Returns a process exit code: 0 on clean shutdown, 1 if startup fails.
    """
    args = _parse_args(argv)
    config = Config.from_env()
    logger = Logger(
        log_file=config.log_file,
        level=config.log_level,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    install_bash_format_on_root()  # make stray docker-py/urllib3 logs match our format
    # Opt-in notifications (#22). Built early so the "refused to start" paths below
    # can alert too; NullNotifier (no-op) unless APPRISE_URLS is configured.
    notifier = build_notifier(config, logger)

    # The diagnostic sub-commands below run before the fatal-config gate, so a
    # malformed env would otherwise be silently substituted with defaults and the
    # output judged against the wrong values. Surface the errors first.
    if config.errors and (args.notify_test or args.suggest_tunables):
        for err in config.errors:
            logger.warn(err)
        logger.warn("Config has errors (above); diagnostic output may use defaults.")

    if args.notify_test:
        return _run_notify_test(config, notifier, logger)

    if args.suggest_tunables:
        return _run_suggest_tunables(config, logger)

    _install_signal_handlers(logger)
    _announce_banner(config, logger)
    _log_notify_summary(config, logger)

    if config.errors:  # malformed env values — fatal; don't run with unparseable params
        for err in config.errors:
            logger.error(err)
        logger.error("Refusing to start due to invalid configuration")
        notifier.notify(NotifyEvent(
            "attention", "gluetun-monitor refused to start",
            "Invalid configuration: " + "; ".join(config.errors), key="refused-config",
        ))
        return 1

    try:
        client: DockerClient = DockerPyClient(timeout=_transport_timeout(config))
    except Exception as exc:
        logger.error(f"Failed to initialize Docker client: {exc}")
        notifier.notify(NotifyEvent(
            "attention", "gluetun-monitor refused to start",
            f"Failed to initialize the Docker client: {exc}", key="refused-docker",
        ))
        return 1

    if not check_prerequisites(client, config, logger):
        notifier.notify(NotifyEvent(
            "attention", "gluetun-monitor refused to start",
            "Prerequisite check failed — see the logs for the specific cause.", key="refused-prereq",
        ))
        return 1

    monitor = Monitor(client, config, logger, notifier=notifier)
    monitor.announce()
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
