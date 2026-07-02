"""In-memory fake DockerClient + inspect-dict builder for unit tests.

The fake records every mutating call (restart/remove/create/start) so tests can
assert *what the monitor decided to do* without a live daemon — this is the seam
ADR-0007 exists to provide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gluetun_monitor.docker_client import ContainerInfo, ExecResult
from gluetun_monitor.notify import NotifyEvent

ExecHandler = Callable[[str, list[str]], ExecResult]


class FakeNotifier:
    """Records every NotifyEvent so tests can assert what was pushed (no network)."""

    def __init__(self) -> None:
        self.events: list[NotifyEvent] = []  # flattened across batches
        self.batches: list[list[NotifyEvent]] = []  # one entry per flush (rollup)
        self.test_result = True

    def notify_batch(self, events: list[NotifyEvent]) -> None:
        self.events.extend(events)
        self.batches.append(list(events))

    def notify(self, event: NotifyEvent) -> None:
        self.notify_batch([event])

    def test(self) -> bool:
        return self.test_result

    def event_keys(self) -> list[str]:
        """The dedup keys of recorded events, in order — handy for assertions."""
        return [e.key for e in self.events]


def make_inspect(
    name: str,
    *,
    id: str,
    network_mode: str = "",
    running: bool = True,
    health: str | None = "healthy",
    mounts: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    host_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a docker-inspect-shaped dict."""
    state: dict[str, Any] = {"Running": running}
    if health is not None:
        state["Health"] = {"Status": health}
    full_host_config = {"NetworkMode": network_mode}
    full_host_config.update(host_config or {})
    return {
        "Id": id,
        "Name": f"/{name}",
        "State": state,
        "Config": config or {"Image": "example/app:latest"},
        "HostConfig": full_host_config,
        "Mounts": mounts or [],
    }


class FakeDockerClient:
    """Scriptable in-memory DockerClient."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}  # id -> raw inspect
        self.ping_ok = True
        self.on_exec: ExecHandler = lambda name, cmd: ExecResult(0, "")
        # Recorded mutations:
        self.restarted: list[str] = []
        self.removed: list[tuple[str, bool]] = []  # (name_or_id, volumes)
        self.created: list[tuple[dict[str, Any], str]] = []  # (body, name)
        self.started: list[str] = []
        self.renamed: list[tuple[str, str]] = []  # (name_or_id, new_name)
        self.ensured_timeouts: list[int] = []  # every ensure_timeout() call (#77)
        self.logs_tails: list[int] = []  # tail= of every logs() call (#78)
        self._id_seq = 1000

    # ----- test setup helpers -----

    def add(self, raw: dict[str, Any]) -> ContainerInfo:
        self._store[raw["Id"]] = raw
        return ContainerInfo.from_inspect(raw)

    def add_container(self, name: str, **kwargs: Any) -> ContainerInfo:
        if "id" not in kwargs:
            kwargs["id"] = f"id_{name}_{self._next_id()}"
        return self.add(make_inspect(name, **kwargs))

    def _next_id(self) -> int:
        self._id_seq += 1
        return self._id_seq

    def _resolve(self, name_or_id: str) -> dict[str, Any] | None:
        if name_or_id in self._store:
            return self._store[name_or_id]
        for raw in self._store.values():
            if raw["Name"].lstrip("/") == name_or_id:
                return raw
            if raw["Id"].startswith(name_or_id) and len(name_or_id) >= 12:
                return raw
        return None

    # ----- DockerClient protocol -----

    def ping(self) -> bool:
        return self.ping_ok

    def list_running_ids(self) -> list[str]:
        return [
            raw["Id"]
            for raw in self._store.values()
            if raw.get("State", {}).get("Running", False)
        ]

    def list_all_ids(self) -> list[str]:
        return list(self._store.keys())

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        raw = self._resolve(name_or_id)
        return ContainerInfo.from_inspect(raw) if raw is not None else None

    def exec_run(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        return self.on_exec(name_or_id, cmd)

    def logs(self, name_or_id: str, *, tail: int) -> str:
        # Honor the window like the daemon does (last N lines) and record the
        # requested tail so tests can assert the fetch is bounded (#78).
        self.logs_tails.append(tail)
        raw = self._resolve(name_or_id)
        text = raw.get("_logs", "") if raw else ""
        lines = text.splitlines(keepends=True)
        return "".join(lines[-tail:]) if len(lines) > tail else text

    def restart(self, name_or_id: str) -> None:
        raw = self._resolve(name_or_id)
        if raw is None:
            raise RuntimeError(f"no such container: {name_or_id}")
        raw["State"]["Running"] = True
        self.restarted.append(name_or_id)

    def remove(self, name_or_id: str, *, volumes: bool) -> None:
        raw = self._resolve(name_or_id)
        if raw is None:
            raise RuntimeError(f"no such container: {name_or_id}")
        del self._store[raw["Id"]]
        self.removed.append((name_or_id, volumes))

    def create_from_config(self, config: dict[str, Any], name: str) -> str:
        new_id = f"id_{name}_recreated_{self._next_id()}"
        nm = config.get("HostConfig", {}).get("NetworkMode", "")
        raw = make_inspect(name, id=new_id, network_mode=nm, running=False, health="healthy")
        raw["Config"] = {k: v for k, v in config.items() if k != "HostConfig"}
        raw["HostConfig"] = config.get("HostConfig", {})
        self._store[new_id] = raw
        self.created.append((config, name))
        return new_id

    def rename(self, name_or_id: str, new_name: str) -> None:
        raw = self._resolve(name_or_id)
        if raw is None:
            raise RuntimeError(f"no such container: {name_or_id}")
        if self._resolve(new_name) is not None:
            raise RuntimeError(f"name already in use: {new_name}")
        raw["Name"] = f"/{new_name}"
        self.renamed.append((name_or_id, new_name))

    def start(self, name_or_id: str) -> None:
        raw = self._resolve(name_or_id)
        if raw is None:
            raise RuntimeError(f"no such container: {name_or_id}")
        raw["State"]["Running"] = True
        self.started.append(name_or_id)

    def ensure_timeout(self, seconds: int) -> None:
        self.ensured_timeouts.append(seconds)
