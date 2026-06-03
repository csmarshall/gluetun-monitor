"""Recovery actions: gluetun restart + dependent remediation.

Gluetun recovery is the v1.x path (restart, wait healthy, wait DNS, re-verify).
Dependent remediation applies the ADR-0004 decision: restart for a same-id
strand, recreate for a moved-id strand, try-restart-then-escalate for a
name-form target. Every action ends in a verify (node 19); a disabled/denied
recreate is a FAILED outcome (node 21).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .dependents import interface_check, remediation_action
from .endpoint import get_endpoint_info
from .recreate import recreate_dependent
from .state import InterfaceStatus, RemediationAction

if TYPE_CHECKING:
    from .config import Config
    from .docker_client import DockerClient
    from .logging_setup import Logger

Sleep = Callable[[float], None]


def get_health(client: DockerClient, container: str) -> str:
    """Health status string for ``container`` ('healthy'/'starting'/.../'unknown')."""
    info = client.inspect(container)
    return info.health if info else "unknown"


def wait_for_healthy(
    client: DockerClient,
    container: str,
    max_wait: int,
    logger: Logger,
    *,
    sleep: Sleep = time.sleep,
) -> bool:
    """Poll until ``container`` reports healthy, or ``max_wait`` seconds elapse."""
    logger.info(f"Waiting for {container} to become healthy (max {max_wait}s)...")
    waited = 0
    while waited < max_wait:
        health = get_health(client, container)
        if health == "healthy":
            logger.info(f"{container} is healthy after {waited}s")
            return True
        sleep(5)
        waited += 5
        logger.debug(f"Waiting... ({waited}/{max_wait}s) - current status: {health}")
    logger.error(f"{container} did not become healthy within {max_wait}s")
    return False


def wait_for_dns(
    client: DockerClient,
    container: str,
    max_wait: int,
    logger: Logger,
    *,
    request_timeout: int = 10,
    tries: int = 1,
    sleep: Sleep = time.sleep,
) -> bool:
    """Wait for gluetun DNS + connectivity to stabilize after a restart.

    The per-request probe uses the same standardized TIMEOUT/WGET_TRIES as every
    other wget in the monitor (``request_timeout``/``tries``) — not a hardcoded
    value — so one knob governs them all. ``max_wait`` (DNS_WAIT_TIMEOUT) is the
    separate overall poll budget.
    """
    logger.info("Waiting for DNS to stabilize...")
    waited = 0
    while waited < max_wait:
        try:
            ns = client.exec_run(container, ["nslookup", "google.com", "127.0.0.1"])
            if ns.exit_code == 0:
                probe = client.exec_run(
                    container,
                    ["wget", "--spider", f"--timeout={request_timeout}", f"--tries={tries}",
                     "-q", "https://1.1.1.1"],
                )
                if probe.exit_code == 0:
                    logger.info(f"DNS and connectivity verified after {waited}s")
                    return True
        except Exception:
            pass
        sleep(2)
        waited += 2
        logger.debug(f"Waiting for DNS... ({waited}/{max_wait}s)")
    logger.warn(f"DNS stabilization timeout after {max_wait}s - proceeding anyway")
    return False


def restart_gluetun(
    client: DockerClient,
    config: Config,
    logger: Logger,
    *,
    sleep: Sleep = time.sleep,
) -> bool:
    """Restart gluetun to force a new endpoint, then wait for it to recover."""
    logger.info(f"Restarting {config.gluetun_container} to force new endpoint...")
    failing = get_endpoint_info(client, config.gluetun_container)
    logger.endpoint(failing.format("FAILING", "Site connectivity test failed"))

    try:
        client.restart(config.gluetun_container)
    except Exception as exc:
        logger.error(f"Failed to restart {config.gluetun_container}: {exc}")
        return False

    if not wait_for_healthy(
        client, config.gluetun_container, config.healthy_wait_timeout, logger, sleep=sleep
    ):
        logger.error("Gluetun failed to become healthy after restart")
        return False

    # Best-effort settle: a DNS timeout here logs but does not abort recovery
    # (we proceed and let the post-restart re-verify be the authority), so the
    # bool return is intentionally not consumed.
    wait_for_dns(client, config.gluetun_container, config.dns_wait_timeout, logger,
                 request_timeout=config.timeout, tries=config.wget_tries, sleep=sleep)
    new = get_endpoint_info(client, config.gluetun_container)
    logger.endpoint(new.format("NEW", "After restart"))
    return True


def verify_dependent(client: DockerClient, dep_name: str) -> bool:
    """A dependent is healthy if it is running and not loopback-stranded (node 19)."""
    info = client.inspect(dep_name)
    if info is None or not info.running:
        return False
    return interface_check(client, dep_name) != InterfaceStatus.STRANDED


def _do_restart(client: DockerClient, dep_name: str, logger: Logger, *, sleep: Sleep) -> bool:
    try:
        client.restart(dep_name)
    except Exception as exc:
        logger.error(f"{dep_name} restart raised: {exc}")
        return False
    sleep(2)  # brief settle before the verify, as v1.x did
    return True


def _maybe_recreate(
    client: DockerClient,
    dep_name: str,
    gluetun_id: str,
    config: Config,
    logger: Logger,
) -> bool:
    if not config.auto_recreate:
        logger.error(
            f"{dep_name} needs recreate (gluetun id changed) but AUTO_RECREATE is "
            f"disabled — FAILED, manual intervention required"
        )
        return False
    return recreate_dependent(client, dep_name, gluetun_id, logger)


def remediate_dependent(
    client: DockerClient,
    dep_name: str,
    gluetun_id: str,
    config: Config,
    logger: Logger,
    *,
    sleep: Sleep = time.sleep,
) -> bool:
    """Remediate a failing dependent and verify recovery. Returns success."""
    info = client.inspect(dep_name)
    if info is None:
        logger.error(f"Cannot remediate {dep_name}: container not found")
        return False

    action = remediation_action(info, gluetun_id)

    if action is RemediationAction.RESTART:
        logger.info(f"Restarting {dep_name} (same netns id)")
        if not _do_restart(client, dep_name, logger, sleep=sleep):
            return False
    elif action is RemediationAction.TRY_RESTART:
        logger.info(f"Restarting {dep_name} (name-form netns)")
        if not _do_restart(client, dep_name, logger, sleep=sleep):
            logger.warn(f"{dep_name} restart failed; escalating to recreate")
            if not _maybe_recreate(client, dep_name, gluetun_id, config, logger):
                return False
    elif action is RemediationAction.RECREATE:
        logger.warn(f"{dep_name} netns moved (gluetun recreated) → recreate")
        if not _maybe_recreate(client, dep_name, gluetun_id, config, logger):
            return False

    ok = verify_dependent(client, dep_name)
    if ok:
        logger.info(f"{dep_name} verified healthy after remediation")
    else:
        logger.error(f"{dep_name} still unhealthy after remediation — FAILED")
    return ok
