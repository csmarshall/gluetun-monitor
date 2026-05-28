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
        """True if the daemon/proxy is reachable."""
        ...

    def list_running_ids(self) -> list[str]:
        """IDs of currently running containers."""
        ...

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        """Inspect a container, or None if it does not exist."""
        ...

    def exec_run(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        """Run ``cmd`` inside a container; return its exit code + combined output."""
        ...

    def logs(self, name_or_id: str) -> str:
        """Fetch a container's logs (stdout+stderr) as text."""
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

    def start(self, name_or_id: str) -> None:
        """Start an existing (created) container."""
        ...


class DockerPyClient:
    """``DockerClient`` over docker-py's low-level API (honors DOCKER_HOST)."""

    def __init__(self, timeout: int = 60) -> None:
        import docker  # imported lazily so the fake-only test path needs no daemon

        self._client = docker.from_env(timeout=timeout)
        self._api = self._client.api

    def ping(self) -> bool:
        try:
            return bool(self._api.ping())
        except Exception:
            return False

    def list_running_ids(self) -> list[str]:
        return [c["Id"] for c in self._api.containers(all=False)]

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        import docker.errors

        try:
            raw = self._api.inspect_container(name_or_id)
        except docker.errors.NotFound:
            return None
        return ContainerInfo.from_inspect(raw)

    def exec_run(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        exec_id = self._api.exec_create(name_or_id, cmd)["Id"]
        raw_output = self._api.exec_start(exec_id)
        output = raw_output.decode("utf-8", errors="replace") if raw_output else ""
        exit_code = self._api.exec_inspect(exec_id).get("ExitCode")
        # A still-running/unknown exec reports None; treat as a generic failure.
        return ExecResult(exit_code=exit_code if exit_code is not None else 1, output=output)

    def logs(self, name_or_id: str) -> str:
        raw = self._api.logs(name_or_id, stdout=True, stderr=True)
        return raw.decode("utf-8", errors="replace") if raw else ""

    def restart(self, name_or_id: str) -> None:
        self._api.restart(name_or_id)

    def remove(self, name_or_id: str, *, volumes: bool) -> None:
        self._api.remove_container(name_or_id, v=volumes, force=True)

    def create_from_config(self, config: dict[str, Any], name: str) -> str:
        result = self._api.create_container_from_config(config, name=name)
        return str(result["Id"])

    def start(self, name_or_id: str) -> None:
        self._api.start(name_or_id)
