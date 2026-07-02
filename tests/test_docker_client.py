"""DockerPyClient adapter logic — the translation we own, not docker-py itself.

Constructed via __new__ with a stub low-level API so these stay hermetic (no
daemon, no real docker-py client). What's asserted here is *our* adaptation:
NotFound -> None, exec output decode + exit-code fallback, ping swallows errors.
"""

from __future__ import annotations

from typing import Any

import docker.errors

from gluetun_monitor.docker_client import DockerPyClient


class _StubAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.ping_raises = False
        self.inspect_raises_notfound = False
        self.exec_exit_code: int | None = 0
        self.exec_output: bytes | None = b"out"

    def ping(self) -> bool:
        if self.ping_raises:
            raise RuntimeError("down")
        return True

    def containers(self, all: bool = False) -> list[dict[str, Any]]:
        return [{"Id": "id1"}, {"Id": "id2"}]

    def inspect_container(self, name: str) -> dict[str, Any]:
        if self.inspect_raises_notfound:
            raise docker.errors.NotFound("nope")
        return {"Id": name, "Name": f"/{name}", "State": {"Running": True},
                "HostConfig": {"NetworkMode": "bridge"}}

    def exec_create(self, name: str, cmd: list[str]) -> dict[str, str]:
        self.calls.append(("exec_create", (name, tuple(cmd))))
        return {"Id": "exec1"}

    def exec_start(self, exec_id: str) -> bytes | None:
        return self.exec_output

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self.exec_exit_code}

    def logs(self, name: str, stdout: bool = True, stderr: bool = True,
             tail: int | str = "all") -> bytes:
        self.logs_tail = tail
        return b"log line"

    def restart(self, name: str) -> None:
        self.calls.append(("restart", (name,)))

    def remove_container(self, name: str, v: bool = False, force: bool = False) -> None:
        self.calls.append(("remove_container", (name, v, force)))

    def create_container_from_config(self, config: dict[str, Any], name: str) -> dict[str, str]:
        self.calls.append(("create", (name,)))
        return {"Id": "newid"}

    def start(self, name: str) -> None:
        self.calls.append(("start", (name,)))


def _client() -> tuple[DockerPyClient, _StubAPI]:
    client = DockerPyClient.__new__(DockerPyClient)  # skip __init__ (no daemon)
    api = _StubAPI()
    client._api = api  # type: ignore[attr-defined]
    return client, api


def test_ping_true() -> None:
    """ping() reports reachable when the API answers."""
    client, _ = _client()
    assert client.ping() is True


def test_ping_swallows_errors() -> None:
    """A transport error from ping is swallowed → False (unreachable), not raised
    — prereqs turn that into a clean exit, not a stack trace."""
    client, api = _client()
    api.ping_raises = True
    assert client.ping() is False


def test_list_running_ids() -> None:
    """The container list is flattened to just the ids the monitor uses."""
    client, _ = _client()
    assert client.list_running_ids() == ["id1", "id2"]


def test_inspect_returns_container_info() -> None:
    """inspect maps the raw payload into the normalized ContainerInfo."""
    client, _ = _client()
    info = client.inspect("gluetun")
    assert info is not None
    assert info.name == "gluetun"
    assert info.running is True


def test_inspect_notfound_returns_none() -> None:
    """A NotFound is translated to None (not an exception) — callers test for
    None to mean "doesn't exist"."""
    client, api = _client()
    api.inspect_raises_notfound = True
    assert client.inspect("ghost") is None


def test_exec_run_decodes_output_and_exit_code() -> None:
    """exec output is decoded with errors='replace' (an undecodable byte won't
    crash a probe) and the real exit code is returned."""
    client, api = _client()
    api.exec_output = b"hello\xff"  # includes an undecodable byte
    api.exec_exit_code = 7
    result = client.exec_run("c", ["wget", "x"])
    assert result.exit_code == 7
    assert "hello" in result.output  # decoded with errors="replace", no crash


def test_exec_run_none_exit_code_falls_back_to_failure() -> None:
    """A None/unknown exit code is treated as failure (1), not success — an
    indeterminate probe must not read as healthy (Tenet 7)."""
    client, api = _client()
    api.exec_exit_code = None  # still-running / unknown
    assert client.exec_run("c", ["x"]).exit_code == 1


def test_exec_run_empty_output() -> None:
    """No output decodes to "" rather than None — callers can string-parse safely."""
    client, api = _client()
    api.exec_output = None
    assert client.exec_run("c", ["x"]).output == ""


def test_logs_decodes_and_passes_the_tail_bound() -> None:
    """logs() returns decoded text (the endpoint parser consumes a str) and
    forwards the required tail= window to the API — never an unbounded fetch (#78)."""
    client, api = _client()
    assert client.logs("c", tail=500) == "log line"
    assert api.logs_tail == 500


def test_restart_remove_create_start_delegate() -> None:
    """The mutating ops map to the right docker-py calls — notably remove passes
    force=True with v=volumes, so the recreate path's "rm without -v" is honored."""
    client, api = _client()
    client.restart("c")
    client.remove("c", volumes=False)
    new_id = client.create_from_config({"HostConfig": {}}, "c")
    client.start(new_id)
    assert ("restart", ("c",)) in api.calls
    assert ("remove_container", ("c", False, True)) in api.calls  # force=True, v=volumes
    assert new_id == "newid"
    assert ("start", ("newid",)) in api.calls
