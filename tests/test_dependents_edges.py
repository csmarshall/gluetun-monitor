"""Edge/exception branches in discovery and the remediation decision."""

from __future__ import annotations

from gluetun_monitor.dependents import (
    discover_dependents,
    interface_check,
    remediation_action,
)
from gluetun_monitor.state import InterfaceStatus, RemediationAction

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_discover_returns_empty_when_gluetun_absent() -> None:
    fake = FakeDockerClient()  # no gluetun
    assert discover_dependents(fake, "gluetun") == []


def test_interface_check_unknown_when_exec_raises() -> None:
    fake = FakeDockerClient()

    def boom(name: str, cmd: list[str]):
        raise RuntimeError("no such exec target")

    fake.on_exec = boom
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_remediation_empty_target_is_try_restart() -> None:
    fake = FakeDockerClient()
    info = fake.add_container("dep", network_mode="container:")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.TRY_RESTART
