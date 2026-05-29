"""Defensive exception / malformed-input branches across modules."""

from __future__ import annotations

import io

from gluetun_monitor.config import Config
from gluetun_monitor.dependents import remediation_action
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.endpoint import get_endpoint_info
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recovery import remediate_dependent, restart_gluetun, wait_for_dns
from gluetun_monitor.recreate import build_create_body, recreate_dependent
from gluetun_monitor.state import RemediationAction

from .fakes import FakeDockerClient, make_inspect

GLUETUN_ID = "a" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


# ----- endpoint: logs() raising -----


def test_get_endpoint_info_handles_logs_exception() -> None:
    fake = FakeDockerClient()

    def boom(name: str) -> str:
        raise RuntimeError("logs unavailable")

    fake.logs = boom  # type: ignore[method-assign]
    info = get_endpoint_info(fake, "gluetun")
    assert info.public_ip == "unknown"  # default, no crash


# ----- dependents: empty gluetun id reaches _ids_match guard -----


def test_remediation_recreate_when_gluetun_id_empty() -> None:
    fake = FakeDockerClient()
    info = fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    # An empty current id can't match -> recreate (exercises the _ids_match guard).
    assert remediation_action(info, "") is RemediationAction.RECREATE


# ----- recreate: malformed mounts are skipped -----


def test_build_create_body_skips_malformed_mounts() -> None:
    raw = make_inspect(
        "dep",
        id="d1",
        network_mode=f"container:{GLUETUN_ID}",
        mounts=[
            {"Type": "volume", "Destination": "/a"},          # volume w/o Name -> skip
            {"Type": "bind", "Destination": "/b"},            # bind w/o Source -> skip
            {"Type": "weird", "Destination": "/c"},           # unknown type -> skip
            {"Type": "volume", "Name": "v", "Destination": ""},  # no destination -> skip
            {"Type": "volume", "Name": "ok", "Destination": "/d"},  # kept
        ],
    )
    hc = build_create_body(raw, "f" * 64)["HostConfig"]
    assert hc["Mounts"] == [{"Type": "volume", "Source": "ok", "Target": "/d", "ReadOnly": False}]


# ----- recreate: create failure -> False (and the old container is already gone) -----


def test_recreate_dependent_create_failure_returns_false() -> None:
    fake = FakeDockerClient()
    fake.add(make_inspect("dep", id="d1", network_mode=f"container:{GLUETUN_ID}"))

    def boom(config: dict[str, object], name: str) -> str:
        raise RuntimeError("create rejected")

    fake.create_from_config = boom  # type: ignore[method-assign]
    assert recreate_dependent(fake, "dep", "f" * 64, _logger()) is False


# ----- recovery: restart_gluetun when restart raises -----


def test_restart_gluetun_handles_restart_exception() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def boom(name: str) -> None:
        raise RuntimeError("daemon refused")

    fake.restart = boom  # type: ignore[method-assign]
    assert restart_gluetun(fake, Config(), _logger(), sleep=lambda _s: None) is False


# ----- recovery: wait_for_dns swallows exec exceptions and times out -----


def test_wait_for_dns_handles_exec_exception() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def boom(name: str, cmd: list[str]) -> ExecResult:
        raise RuntimeError("exec failed")

    fake.on_exec = boom
    assert wait_for_dns(fake, "gluetun", 4, _logger(), sleep=lambda _s: None) is False


# ----- recovery: remediate when the container vanished -----


def test_remediate_missing_container_returns_false() -> None:
    fake = FakeDockerClient()
    ok = remediate_dependent(fake, "ghost", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is False


# ----- recovery: try-restart succeeds (no escalation) -----


def test_remediate_try_restart_success_without_escalation() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode="container:gluetun")  # name form -> TRY_RESTART

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        return ExecResult(0, "")

    fake.on_exec = handler
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert fake.restarted == ["dep"]
    assert fake.created == []  # restart worked, no escalation to recreate
