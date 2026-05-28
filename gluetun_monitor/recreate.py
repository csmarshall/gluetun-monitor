"""Non-destructive recreate of a stranded dependent (ADR-0005).

When gluetun is recreated it gets a **new container id**; a dependent whose
``NetworkMode`` is ``container:<old-id>`` can no longer be restarted into the
(now absent) namespace — ``NetworkMode`` is immutable, so the dependent must be
**recreated** pointing at the new id.

``build_create_body`` is the load-bearing transform and is intentionally pure
(takes an inspect dict, returns an API create body — no Docker calls) so the
data-loss guards are unit-testable:

* re-point ``NetworkMode`` at the new gluetun id,
* strip the fields Docker forbids when sharing another container's netns
  (hostname/dns/ports — they belong to gluetun now),
* carry every existing mount forward by **source**, including anonymous volumes
  (bind the existing volume id to its destination) so a subsequent ``rm`` WITHOUT
  ``-v`` preserves all data; only the ephemeral writable layer is lost.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .docker_client import DockerClient
    from .logging_setup import Logger

# Config fields the daemon rejects (or that are meaningless) when NetworkMode is
# container:<id> — the shared netns owns the hostname and the port surface.
_STRIP_CONFIG_FIELDS = ("Hostname", "Domainname", "ExposedPorts", "MacAddress")

# HostConfig fields tied to an owned network stack; invalid in shared-netns mode.
_STRIP_HOSTCONFIG_FIELDS = (
    "PortBindings",
    "PublishAllPorts",
    "Dns",
    "DnsOptions",
    "DnsSearch",
    "ExtraHosts",
    "Links",
)


def _mount_spec(mount: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one inspect ``Mounts`` entry into a create-body mount spec.

    Volumes are carried by their **existing name** (the same underlying volume,
    so data survives); binds by host source; tmpfs by target only. Returns None
    for entries we can't faithfully reproduce.
    """
    mtype = mount.get("Type")
    destination = mount.get("Destination")
    if not destination:
        return None
    read_only = not mount.get("RW", True)

    if mtype == "volume":
        source = mount.get("Name")
        if not source:
            return None
        return {"Type": "volume", "Source": source, "Target": destination, "ReadOnly": read_only}
    if mtype == "bind":
        source = mount.get("Source")
        if not source:
            return None
        return {"Type": "bind", "Source": source, "Target": destination, "ReadOnly": read_only}
    if mtype == "tmpfs":
        return {"Type": "tmpfs", "Target": destination}
    return None


def build_create_body(inspect_raw: dict[str, Any], new_gluetun_id: str) -> dict[str, Any]:
    """Build a Docker API create body that re-homes a dependent into a new netns.

    Pure: no Docker calls. ``inspect_raw`` is a full ``docker inspect`` payload;
    the return value is suitable for ``create_container_from_config``.
    """
    config: dict[str, Any] = copy.deepcopy(inspect_raw.get("Config", {}) or {})
    host_config: dict[str, Any] = copy.deepcopy(inspect_raw.get("HostConfig", {}) or {})

    # 1. Re-home the netns onto the new gluetun container id.
    host_config["NetworkMode"] = f"container:{new_gluetun_id}"

    # 2. Strip fields that conflict with sharing another container's network.
    for f in _STRIP_CONFIG_FIELDS:
        config.pop(f, None)
    for f in _STRIP_HOSTCONFIG_FIELDS:
        host_config.pop(f, None)

    # 3. Carry mounts forward by source (anon volumes included). Mounts is the
    #    authoritative long-form list, so drop the legacy short forms to avoid
    #    Docker minting fresh anonymous volumes for the same destinations.
    mounts: list[dict[str, Any]] = []
    for m in inspect_raw.get("Mounts", []) or []:
        spec = _mount_spec(m)
        if spec is not None:
            mounts.append(spec)
    if mounts:
        host_config["Mounts"] = mounts
    else:
        host_config.pop("Mounts", None)
    host_config.pop("Binds", None)
    host_config.pop("VolumesFrom", None)
    # Config.Volumes would otherwise trigger fresh anon volumes for image VOLUMEs
    # we've already carried via Mounts.
    config.pop("Volumes", None)

    # 4. Assemble the API create body: Config fields at top level + HostConfig.
    body: dict[str, Any] = dict(config)
    body["HostConfig"] = host_config
    # A shared netns ignores per-container network config; don't send stale data.
    body.pop("NetworkingConfig", None)
    return body


def recreate_dependent(
    client: DockerClient,
    dep_name: str,
    new_gluetun_id: str,
    logger: Logger,
) -> bool:
    """Recreate ``dep_name`` re-homed onto ``new_gluetun_id``. Returns success.

    Order is remove-then-create because the new container reuses the same name.
    ``volumes=False`` on remove is the data-preservation guarantee (ROC, ADR-0005).
    """
    info = client.inspect(dep_name)
    if info is None:
        logger.error(f"Cannot recreate {dep_name}: container not found")
        return False

    try:
        body = build_create_body(info.raw, new_gluetun_id)
    except Exception as exc:
        logger.error(f"Cannot recreate {dep_name}: failed to build spec: {exc}")
        return False

    logger.warn(f"Recreating {dep_name} (re-homing netns onto gluetun {new_gluetun_id[:12]})")
    try:
        client.remove(dep_name, volumes=False)  # preserve named + anonymous volumes
        new_id = client.create_from_config(body, name=dep_name)
        client.start(new_id)
    except Exception as exc:
        logger.error(f"Recreate of {dep_name} failed: {exc}")
        return False

    logger.info(f"{dep_name} recreated as {new_id[:12]} and started")
    return True
