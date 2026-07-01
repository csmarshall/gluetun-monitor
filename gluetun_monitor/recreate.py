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
import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .docker_client import DockerClient
    from .logging_setup import Logger

# While a recreate is mid-flight the original container is parked under this
# suffix (rename-aside), so every intermediate state is recoverable (#76).
# Docker name charset allows '.', and the suffix is specific enough not to
# collide with a real container name.
_OLD_SUFFIX = ".gm-recreate-old"

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


@contextmanager
def _defer_stop_signals(logger: Logger) -> Iterator[None]:
    """Hold SIGTERM/SIGINT for the duration of the recreate critical section.

    The CLI's handlers exit immediately; a stop signal landing mid-recreate
    (e.g. ``docker compose up -d`` recreating the monitor itself) would strand
    the sequence half-done (#76). Signals received while held are re-delivered
    to the restored handlers on exit, so shutdown still happens — just after
    the container work is consistent. No-op off the main thread (CPython only
    delivers signals to the main thread, and ``signal.signal`` would raise).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []

    def _hold(signum: int, _frame: object) -> None:
        if signum not in pending:
            pending.append(signum)
        logger.warn(
            f"Received {signal.Signals(signum).name} during a recreate — "
            f"deferring shutdown until the container is consistent"
        )

    previous = {s: signal.signal(s, _hold) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        for signum in pending:
            os.kill(os.getpid(), signum)  # re-deliver to the restored handler


def gc_recreate_leftover(client: DockerClient, dep_name: str, logger: Logger) -> bool:
    """Remove a parked ``<dep>.gm-recreate-old`` left by an interrupted recreate.

    Called before ANY remediation of ``dep_name``: if the leftover still exists
    alongside the (already-replaced or about-to-be-replaced) dependent, it holds
    the same volume mounts — starting/recreating the dependent with it still
    around risks two instances writing the same data. Returns False when a
    leftover exists but cannot be removed (the caller must NOT proceed).
    """
    leftover = f"{dep_name}{_OLD_SUFFIX}"
    if client.inspect(leftover) is None:
        return True
    try:
        client.remove(leftover, volumes=False)
    except Exception as exc:
        logger.error(
            f"Cannot remove leftover {leftover} from an interrupted recreate: {exc} "
            f"— refusing to remediate {dep_name} while it exists (shared volumes)"
        )
        return False
    logger.warn(f"Removed leftover {leftover} from a previously interrupted recreate")
    return True


def sweep_recreate_leftovers(client: DockerClient, logger: Logger) -> None:
    """Startup recovery for a recreate interrupted by SIGKILL/power loss (#76).

    A ``<dep>.gm-recreate-old`` container found at startup means a recreate died
    mid-sequence. If the real name is free (interrupted between rename and
    create), the parked container IS the workload — rename it back. If the real
    name exists (replacement was created), the parked one is condemned — remove
    it (volumes preserved; they belong to the replacement now).
    """
    for cid in client.list_running_ids():
        info = client.inspect(cid)
        if info is None or not info.name.endswith(_OLD_SUFFIX):
            continue
        original = info.name[: -len(_OLD_SUFFIX)]
        if client.inspect(original) is None:
            try:
                client.rename(info.name, original)
                logger.warn(
                    f"Restored {original}: an interrupted recreate had left it "
                    f"parked as {info.name}"
                )
            except Exception as exc:
                logger.error(f"Could not restore {info.name} to {original}: {exc}")
        else:
            try:
                client.remove(info.name, volumes=False)
                logger.warn(
                    f"Removed {info.name}: leftover from an interrupted recreate "
                    f"({original} was already replaced)"
                )
            except Exception as exc:
                logger.error(f"Could not remove leftover {info.name}: {exc}")


def recreate_dependent(
    client: DockerClient,
    dep_name: str,
    new_gluetun_id: str,
    logger: Logger,
) -> bool:
    """Recreate ``dep_name`` re-homed onto ``new_gluetun_id``. Returns success.

    The new container must reuse the same name, but remove-then-create left a
    window where a failure (Docker API error, stop signal) destroyed the
    dependent unrecoverably (#76). Instead: **rename aside → create → remove old
    → start**, under deferred stop signals. Every intermediate state is
    recoverable — a create failure rolls the rename back; anything harder is
    healed by :func:`sweep_recreate_leftovers` at the next start. The old
    container is removed *before* the new one starts because they share every
    mount (two live instances would corrupt the volumes). ``volumes=False`` on
    remove is the data-preservation guarantee (ROC, ADR-0005).
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

    old_name = f"{dep_name}{_OLD_SUFFIX}"
    logger.warn(f"Recreating {dep_name} (re-homing netns onto gluetun {new_gluetun_id[:12]})")
    with _defer_stop_signals(logger):
        # 1. Park the original (still running; fully reversible).
        try:
            client.rename(dep_name, old_name)
        except Exception as exc:
            logger.error(f"Recreate of {dep_name} failed renaming the original: {exc}")
            return False
        # 2. Create the replacement under the real name; roll back on failure.
        try:
            new_id = client.create_from_config(body, name=dep_name)
        except Exception as exc:
            logger.error(f"Recreate of {dep_name} failed at create: {exc} — rolling back")
            try:
                client.rename(old_name, dep_name)
                logger.info(f"{dep_name} restored (recreate rolled back; container unchanged)")
            except Exception as rollback_exc:
                logger.error(
                    f"Rollback rename of {dep_name} also failed: {rollback_exc} — the "
                    f"original is still intact as {old_name}; the startup sweep will "
                    f"restore it"
                )
            return False
        # 3. Retire the original BEFORE starting the replacement — they share
        #    every mount, and two live instances would corrupt the volumes.
        try:
            client.remove(old_name, volumes=False)  # preserve named + anon volumes
        except Exception as exc:
            logger.error(
                f"Recreate of {dep_name}: could not remove the old container: {exc} — "
                f"NOT starting the replacement (shared volumes); will retry next loop"
            )
            return False
        # 4. Start the replacement.
        try:
            client.start(new_id)
        except Exception as exc:
            logger.error(f"Recreate of {dep_name}: created but failed to start: {exc}")
            return False

    logger.info(f"{dep_name} recreated as {new_id[:12]} and started")
    return True
