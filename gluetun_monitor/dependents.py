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


def is_container_id(value: str) -> bool:
    """True if ``value`` looks like a Docker container id (12-64 hex chars), as
    opposed to a container *name* (used to tell id-form NetworkMode targets apart
    from name-form ones).
    """
    return bool(_CONTAINER_ID_RE.match(value))


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
            # DEBUG, not INFO: the per-loop INFO heartbeat already reports the
            # dependent count ("dependents: N/M ok"), so naming them every loop
            # is redundant noise at the default level.
            logger.debug(f"Discovered dependent containers: {','.join(discovered)}")
        else:
            logger.warn("No dependent containers discovered")
        return discovered
    return parse_csv_names(config.dependent_containers)


def parse_csv_names(value: str) -> list[str]:
    """Parse a comma-separated container-name list into trimmed, non-empty names.

    Shared by the manual ``DEPENDENT_CONTAINERS`` list and ``EXCLUDE_CONTAINERS``
    so both interpret the value identically.
    """
    return [n for n in (trim(x) for x in value.split(",")) if n]


def list_interfaces(client: DockerClient, dep_name: str) -> set[str] | None:
    """Return the dependent's network interfaces (``ls /sys/class/net``), or None
    if it couldn't be determined (exec failed / no shell / empty output).
    """
    try:
        result = client.exec_run(dep_name, ["ls", "/sys/class/net"])
    except Exception:
        return None
    if result.exit_code != 0:
        return None
    interfaces = {tok for tok in result.output.split() if tok}
    return interfaces or None


def classify_interfaces(interfaces: set[str] | None) -> InterfaceStatus:
    """LIVE if a non-loopback interface is present, STRANDED if only ``lo``,
    UNKNOWN if we couldn't read them (ADR-0004, node 7).
    """
    if not interfaces:
        return InterfaceStatus.UNKNOWN
    return InterfaceStatus.LIVE if (interfaces - {"lo"}) else InterfaceStatus.STRANDED


def interface_check(client: DockerClient, dep_name: str) -> InterfaceStatus:
    """Classify a dependent by its network interfaces (ADR-0004, node 7)."""
    return classify_interfaces(list_interfaces(client, dep_name))


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
    if is_container_id(target):
        if _ids_match(target, gluetun_id):
            return RemediationAction.RESTART
        return RemediationAction.RECREATE
    # Name form (e.g. container:gluetun): can't tell — try restart, escalate on fail.
    return RemediationAction.TRY_RESTART
