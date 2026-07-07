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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .dependents import interface_check, remediation_action
from .endpoint import get_endpoint_info
from .recreate import Wedge, gc_recreate_leftover, recreate_dependent
from .state import InterfaceStatus, RemediationAction

if TYPE_CHECKING:
    from .config import Config
    from .docker_client import DockerClient
    from .logging_setup import Logger

Sleep = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class RemediationOutcome:
    """Result of one dependent remediation attempt.

    Truthiness == success, so boolean call sites keep reading naturally.
    ``detail`` is the failure signature: the monitor compares it across loops to
    recognize *the same* failure repeating (the wedge-escalation trigger, #98).
    ``wedge`` carries the unremovable-parked-twin specifics when that is the
    blocker, so the escalation alert can ship the runbook.
    """

    ok: bool
    detail: str = ""
    wedge: Wedge | None = None

    def __bool__(self) -> bool:
        """Truthiness is success, so ``if outcome:`` reads like the old bool API."""
        return self.ok


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
    settle: int = 5,
) -> bool:
    """Poll until ``container`` reports healthy, or ``max_wait`` seconds elapse."""
    logger.info(f"Waiting for {container} to become healthy (max {max_wait}s)...")
    waited = 0
    while waited < max_wait:
        health = get_health(client, container)
        if health == "healthy":
            logger.info(f"{container} is healthy after {waited}s")
            return True
        if health == "unknown":
            # No healthcheck defined — 'healthy' will never arrive, so polling would
            # just burn the full timeout and then fire a false "cannot recover" alert
            # every restart (#84). Give it a fixed settle delay and treat the restart
            # as done; the DNS-stability check that follows is the real readiness gate.
            logger.info(f"{container} has no healthcheck; settling {settle}s after restart")
            sleep(settle)
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


def _do_restart(
    client: DockerClient, dep_name: str, logger: Logger, *, sleep: Sleep
) -> str | None:
    """Restart ``dep_name``. Returns None on success, the error detail on failure
    (the caller feeds it into the failure signature — #98).
    """
    try:
        client.restart(dep_name)
    except Exception as exc:
        logger.error(f"{dep_name} restart raised: {exc}")
        return f"restart failed: {exc}"
    sleep(2)  # brief settle before the verify, as v1.x did
    return None


def _maybe_recreate(
    client: DockerClient,
    dep_name: str,
    gluetun_id: str,
    config: Config,
    logger: Logger,
) -> str | None:
    """Recreate ``dep_name`` if allowed. Returns None on success, else the
    failure detail (#98 signature).
    """
    if not config.auto_recreate:
        logger.error(
            f"{dep_name} needs recreate (gluetun id changed) but AUTO_RECREATE is "
            f"disabled — FAILED, manual intervention required"
        )
        return "needs recreate but AUTO_RECREATE is disabled"
    if not recreate_dependent(client, dep_name, gluetun_id, logger):
        return "recreate failed (see log for the failing step)"
    return None


def remediate_dependent(
    client: DockerClient,
    dep_name: str,
    gluetun_id: str,
    config: Config,
    logger: Logger,
    *,
    sleep: Sleep = time.sleep,
) -> RemediationOutcome:
    """Remediate a failing dependent and verify recovery.

    The outcome is truthy on success; on failure it carries the signature the
    monitor uses to spot the same failure repeating loop after loop (#98).
    """
    info = client.inspect(dep_name)
    if info is None:
        logger.error(f"Cannot remediate {dep_name}: container not found")
        return RemediationOutcome(False, "container not found")

    # A parked twin from an interrupted recreate shares every volume with this
    # container — restarting/recreating alongside it risks two writers on the
    # same data. Clear it first; refuse to act if it can't be cleared (#76).
    wedge = gc_recreate_leftover(client, dep_name, logger)
    if wedge is not None:
        return RemediationOutcome(
            False,
            f"unremovable parked twin {wedge.parked_name}: {wedge.error}",
            wedge,
        )

    action = remediation_action(info, gluetun_id)

    if action is RemediationAction.RESTART:
        logger.info(f"Restarting {dep_name} (same netns id)")
        err = _do_restart(client, dep_name, logger, sleep=sleep)
        if err is not None:
            return RemediationOutcome(False, err)
    elif action is RemediationAction.TRY_RESTART:
        logger.info(f"Restarting {dep_name} (name-form netns)")
        if _do_restart(client, dep_name, logger, sleep=sleep) is not None:
            logger.warn(f"{dep_name} restart failed; escalating to recreate")
            err = _maybe_recreate(client, dep_name, gluetun_id, config, logger)
            if err is not None:
                return RemediationOutcome(False, err)
    elif action is RemediationAction.RECREATE:
        logger.warn(f"{dep_name} netns moved (gluetun recreated) → recreate")
        err = _maybe_recreate(client, dep_name, gluetun_id, config, logger)
        if err is not None:
            return RemediationOutcome(False, err)

    if verify_dependent(client, dep_name):
        logger.info(f"{dep_name} verified healthy after remediation")
        return RemediationOutcome(True)
    logger.error(f"{dep_name} still unhealthy after remediation — FAILED")
    return RemediationOutcome(False, "still unhealthy after remediation")
