"""Dependent discovery, interface classification, and the remediation decision.

* **Discovery** (v1.x ``discover_dependent_containers``): containers whose
  ``NetworkMode`` points at gluetun (by name, full id, or short id prefix).
* **Interface check** (ADR-0004, node 7): ``ls /sys/class/net`` -> live vs
  stranded-loopback-only vs unknown (distroless/exec-failed).
* **Remediation decision** (ADR-0004, nodes 14-17): compare a dependent's
  resolved ``NetworkMode`` target to gluetun's current id — same id => restart,
  different id => recreate, name-form/unreadable => try-restart-then-escalate.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .sites import trim
from .state import InterfaceStatus, RemediationAction

if TYPE_CHECKING:
    from .config import Config
    from .docker_client import ContainerInfo, DockerClient
    from .logging_setup import Logger

_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


def _ids_match(a: str, b: str) -> bool:
    """True if two container ids refer to the same container (short/full prefix)."""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def discover_dependents(client: DockerClient, gluetun_container: str) -> list[str]:
    """Names of running containers sharing gluetun's network namespace."""
    gluetun = client.inspect(gluetun_container)
    if gluetun is None:
        return []
    gluetun_id = gluetun.id
    short_id = gluetun_id[:12]

    name_form = f"container:{gluetun_container}"
    id_form = f"container:{gluetun_id}"
    short_prefix = f"container:{short_id}"

    found: list[str] = []
    for cid in client.list_running_ids():
        info = client.inspect(cid)
        if info is None or info.name == gluetun_container:
            continue
        nm = info.network_mode
        if nm in (name_form, id_form) or nm.startswith(short_prefix):
            found.append(info.name)
    return found


def get_dependents(client: DockerClient, config: Config, logger: Logger) -> list[str]:
    """Resolve the dependent set: auto-discovery or the configured manual list."""
    if config.dependent_containers == "auto":
        discovered = discover_dependents(client, config.gluetun_container)
        if discovered:
            logger.info(f"Discovered dependent containers: {','.join(discovered)}")
        else:
            logger.warn("No dependent containers discovered")
        return discovered
    return [d for d in (trim(x) for x in config.dependent_containers.split(",")) if d]


def interface_check(client: DockerClient, dep_name: str) -> InterfaceStatus:
    """Classify a dependent by its network interfaces (ADR-0004, node 7)."""
    try:
        result = client.exec_run(dep_name, ["ls", "/sys/class/net"])
    except Exception:
        return InterfaceStatus.UNKNOWN
    if result.exit_code != 0:
        return InterfaceStatus.UNKNOWN
    interfaces = {tok for tok in result.output.split() if tok}
    if not interfaces:
        return InterfaceStatus.UNKNOWN
    non_loopback = interfaces - {"lo"}
    return InterfaceStatus.LIVE if non_loopback else InterfaceStatus.STRANDED


def remediation_action(dep_info: ContainerInfo, gluetun_id: str) -> RemediationAction:
    """Decide restart vs recreate for a failing dependent (ADR-0004 decision tree).

    Compose's ``network_mode: service:gluetun`` resolves to ``container:<full-id>``;
    that id moving is exactly the recreate trigger.
    """
    nm = dep_info.network_mode
    if not nm.startswith("container:"):
        return RemediationAction.TRY_RESTART
    target = nm.split(":", 1)[1]
    if not target:
        return RemediationAction.TRY_RESTART
    if _CONTAINER_ID_RE.match(target):
        if _ids_match(target, gluetun_id):
            return RemediationAction.RESTART
        return RemediationAction.RECREATE
    # Name form (e.g. container:gluetun): can't tell — try restart, escalate on fail.
    return RemediationAction.TRY_RESTART
