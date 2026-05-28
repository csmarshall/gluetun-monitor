"""Recovery: gluetun health/DNS waits and the dependent remediation matrix."""

from __future__ import annotations

import io

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recovery import (
    remediate_dependent,
    restart_gluetun,
    verify_dependent,
    wait_for_dns,
    wait_for_healthy,
)

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _live_exec(name: str, cmd: list[str]) -> ExecResult:
    """Interface check -> LIVE; everything else -> success."""
    if cmd[:2] == ["ls", "/sys/class/net"]:
        return ExecResult(0, "eth0\nlo\ntun0\n")
    return ExecResult(0, "")


# ----- wait_for_healthy -----


def test_wait_for_healthy_succeeds_after_poll() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="starting")

    def flip_to_healthy(_seconds: float) -> None:
        fake._resolve("gluetun")["State"]["Health"]["Status"] = "healthy"

    assert wait_for_healthy(fake, "gluetun", 60, _logger(), sleep=flip_to_healthy) is True


def test_wait_for_healthy_times_out() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="starting")
    assert wait_for_healthy(fake, "gluetun", 10, _logger(), sleep=lambda _s: None) is False


# ----- wait_for_dns -----


def test_wait_for_dns_succeeds() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(0, "")
    assert wait_for_dns(fake, "gluetun", 30, _logger(), sleep=lambda _s: None) is True


def test_wait_for_dns_times_out_but_proceeds() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(1, "")
    assert wait_for_dns(fake, "gluetun", 4, _logger(), sleep=lambda _s: None) is False


# ----- restart_gluetun -----


def test_restart_gluetun_success() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = lambda name, cmd: ExecResult(0, "")
    cfg = Config()
    assert restart_gluetun(fake, cfg, _logger(), sleep=lambda _s: None) is True
    assert fake.restarted == ["gluetun"]


def test_restart_gluetun_fails_if_never_healthy() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="unhealthy")
    cfg = Config(healthy_wait_timeout=10)
    assert restart_gluetun(fake, cfg, _logger(), sleep=lambda _s: None) is False


# ----- verify_dependent -----


def test_verify_dependent_live() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = _live_exec
    assert verify_dependent(fake, "dep") is True


def test_verify_dependent_stranded_fails() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(0, "lo\n")
    assert verify_dependent(fake, "dep") is False


# ----- remediate_dependent matrix -----


def test_remediate_restart_path() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = _live_exec
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert fake.restarted == ["dep"]
    assert fake.created == []  # restart, not recreate


def test_remediate_recreate_path() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")  # gluetun id moved
    fake.on_exec = _live_exec
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert fake.removed == [("dep", False)]
    assert len(fake.created) == 1
    assert fake.restarted == []  # recreate, not restart


def test_remediate_recreate_disabled_is_failed() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")
    fake.on_exec = _live_exec
    cfg = Config(auto_recreate=False)
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, cfg, _logger(), sleep=lambda _s: None)
    assert ok is False
    assert fake.removed == []  # nothing destroyed when disabled


def test_remediate_try_restart_escalates_to_recreate() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode="container:gluetun")  # name form -> TRY_RESTART
    fake.on_exec = _live_exec

    def failing_restart(_name: str) -> None:
        raise RuntimeError("cannot restart into vanished netns")

    fake.restart = failing_restart  # type: ignore[method-assign]
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert len(fake.created) == 1  # escalated to recreate
