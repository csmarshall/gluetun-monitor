"""Edge/exception branches in discovery and the remediation decision.

Why: these guard the defensive paths — a missing gluetun, an exec that throws,
and a malformed NetworkMode — so an odd container or a transient Docker error
degrades to a safe default instead of crashing the loop or mis-deciding.
"""

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
    """If gluetun can't be inspected, discovery returns [] rather than raising —
    prereqs handle the missing-gluetun error; discovery just no-ops."""
    fake = FakeDockerClient()  # no gluetun
    assert discover_dependents(fake, "gluetun") == []


def test_interface_check_unknown_when_exec_raises() -> None:
    """An exec that throws (not just non-zero) is treated as UNKNOWN, not a crash
    — a transient Docker error must not take down the loop (Tenet 7)."""
    fake = FakeDockerClient()

    def boom(name: str, cmd: list[str]):
        raise RuntimeError("no such exec target")

    fake.on_exec = boom
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_remediation_empty_target_is_try_restart() -> None:
    """A malformed `container:` (empty target) can't be compared to an id, so it
    falls to the safe try-restart branch rather than a recreate."""
    fake = FakeDockerClient()
    info = fake.add_container("dep", network_mode="container:")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.TRY_RESTART
