"""The Docker seam (ADR-0007).

Everything the monitor needs from Docker goes through the ``DockerClient``
Protocol. ``DockerPyClient`` implements it over docker-py's low-level API
(which speaks the same HTTP endpoints the tecnativa socket-proxy gates:
CONTAINERS / POST / EXEC). Tests inject a ``FakeDockerClient`` instead, so the
entire monitor — including the recreate path — is exercised without a daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of an ``exec`` inside a container."""

    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """Normalized view of ``docker inspect`` for one container.

    ``raw`` is the full inspect payload — the recreate path (ADR-0005) reads
    ``Config``/``HostConfig``/``Mounts`` directly from it.
    """

    id: str
    name: str
    network_mode: str
    running: bool
    health: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_inspect(cls, raw: dict[str, Any]) -> ContainerInfo:
        """Build a ContainerInfo from a full ``docker inspect`` payload."""
        state = raw.get("State", {}) or {}
        health_obj = state.get("Health") or {}
        host_config = raw.get("HostConfig", {}) or {}
        return cls(
            id=raw.get("Id", ""),
            name=str(raw.get("Name", "")).lstrip("/"),
            network_mode=str(host_config.get("NetworkMode", "")),
            running=bool(state.get("Running", False)),
            health=str(health_obj.get("Status", "unknown")) if health_obj else "unknown",
            raw=raw,
        )


class DockerClient(Protocol):
    """The minimal Docker surface the monitor depends on."""

    def ping(self) -> bool:
        """True if the daemon/proxy is reachable.

        Uses Docker's ``/_ping`` (docker-py ``ping()``); validated to be permitted
        by the default tecnativa socket-proxy config (CONTAINERS/POST/EXEC) — only
        ``/info`` is gated there (ADR-0002), not ``/_ping``.
        """
        ...

    def list_running_ids(self) -> list[str]:
        """IDs of currently running containers."""
        ...

    def list_all_ids(self) -> list[str]:
        """IDs of ALL containers, including stopped/exited ones.

        The stranded-orphan adoption scan (#97) must see exited containers: a
        dependent stranded by a gluetun recreate is usually driven to Exited by
        its own restart policy (a start onto a dead netns id fails hard), and a
        running-only listing would hide exactly the containers most in need of
        healing.
        """
        ...

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        """Inspect a container, or None if it does not exist."""
        ...

    def exec_run(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        """Run ``cmd`` inside a container; return its exit code + combined output."""
        ...

    def logs(self, name_or_id: str, *, tail: int) -> str:
        """Fetch the last ``tail`` lines of a container's logs (stdout+stderr).

        ``tail`` is required: an unbounded fetch of a long-lived container's
        history is potentially hundreds of MB decoded in memory, landing exactly
        mid-recovery (#78) — every call site must state its window.
        """
        ...

    def restart(self, name_or_id: str) -> None:
        """Restart a container (preserves identity — same container id)."""
        ...

    def remove(self, name_or_id: str, *, volumes: bool) -> None:
        """Remove a container. ``volumes=False`` preserves anonymous volumes."""
        ...

    def create_from_config(self, config: dict[str, Any], name: str) -> str:
        """Create a container from a raw API create body; return the new id."""
        ...

    def rename(self, name_or_id: str, new_name: str) -> None:
        """Rename a container (running or not); raises if ``new_name`` is taken."""
        ...

    def start(self, name_or_id: str) -> None:
        """Start an existing (created) container."""
        ...

    def ensure_timeout(self, seconds: int) -> None:
        """Raise the transport read-timeout to at least ``seconds``; never lower it.

        Per-URL ``|timeout=`` overrides can push a probe's legitimate runtime past
        the transport ceiling sized from the global ``TIMEOUT`` (#77) — the sites
        set reloads every loop, so the ceiling must be able to follow it upward.
        Raise-only: lowering mid-flight could clip a concurrent long probe, and
        a too-generous ceiling costs nothing when everything is fast.
        """
        ...


class DockerPyClient:
    """``DockerClient`` over docker-py's low-level API (honors DOCKER_HOST).

    Method contracts are the ones declared on the ``DockerClient`` Protocol; the
    docstrings here note how each maps onto a docker-py API call.
    """

    def __init__(self, timeout: int = 60) -> None:  # pragma: no cover - needs a live daemon
        import docker  # imported lazily so the fake-only test path needs no daemon

        self._client = docker.from_env(timeout=timeout)
        self._api = self._client.api

    def ping(self) -> bool:
        """``GET /_ping`` — swallow any transport error and report unreachable."""
        try:
            return bool(self._api.ping())
        except Exception:
            return False

    def list_running_ids(self) -> list[str]:
        """``GET /containers/json`` (running only) → their ids."""
        return [c["Id"] for c in self._api.containers(all=False)]

    def list_all_ids(self) -> list[str]:
        """``GET /containers/json?all=1`` (every container) → their ids."""
        return [c["Id"] for c in self._api.containers(all=True)]

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        """``GET /containers/{id}/json`` → ContainerInfo, or None if not found."""
        import docker.errors

        try:
            raw = self._api.inspect_container(name_or_id)
        except docker.errors.NotFound:
            return None
        return ContainerInfo.from_inspect(raw)

    def exec_run(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        """exec_create + exec_start + exec_inspect → exit code + combined output."""
        exec_id = self._api.exec_create(name_or_id, cmd)["Id"]
        raw_output = self._api.exec_start(exec_id)
        output = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        exit_code = self._api.exec_inspect(exec_id).get("ExitCode")
        # A still-running/unknown exec reports None; treat as a generic failure.
        return ExecResult(exit_code=exit_code if exit_code is not None else 1, output=output)

    def logs(self, name_or_id: str, *, tail: int) -> str:
        """``GET /containers/{id}/logs`` (stdout+stderr, last ``tail`` lines)
        decoded to text.
        """
        raw = self._api.logs(name_or_id, stdout=True, stderr=True, tail=tail)
        return raw.decode("utf-8", errors="replace") if raw else ""

    def restart(self, name_or_id: str) -> None:
        """``POST /containers/{id}/restart``."""
        self._api.restart(name_or_id)

    def remove(self, name_or_id: str, *, volumes: bool) -> None:
        """``DELETE /containers/{id}`` (force); ``v=volumes`` controls volume removal."""
        self._api.remove_container(name_or_id, v=volumes, force=True)

    def create_from_config(self, config: dict[str, Any], name: str) -> str:
        """``POST /containers/create`` from a raw API body → the new container id."""
        result = self._api.create_container_from_config(config, name=name)
        return str(result["Id"])

    def rename(self, name_or_id: str, new_name: str) -> None:
        """``POST /containers/{id}/rename`` — same CONTAINERS+POST proxy class as
        create/remove/start (ADR-0002), so no new socket-proxy permission needed.
        """
        self._api.rename(name_or_id, new_name)

    def start(self, name_or_id: str) -> None:
        """``POST /containers/{id}/start``."""
        self._api.start(name_or_id)

    def ensure_timeout(self, seconds: int) -> None:  # pragma: no cover - needs a live daemon
        """Bump docker-py's per-request read-timeout (it reads ``api.timeout`` on
        every call, so mutating it takes effect for subsequent requests).
        """
        self._api.timeout = max(self._api.timeout or 0, seconds)
