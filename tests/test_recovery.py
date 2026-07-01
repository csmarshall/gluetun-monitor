"""Recovery: gluetun health/DNS waits and the dependent remediation matrix.

Why: recovery is where the monitor *acts* on the system, so the branch selection
must be exactly right (ADR-0003/0004): restart when the dependent can rejoin,
recreate when gluetun's id moved, escalate when a name-form target's restart
fails, and refuse (FAILED) when recreate is disabled. The 2x2 of
restart-vs-recreate is the heart of the #20 fix; getting it wrong either fails to
heal or churns containers needlessly (Tenets 1, 5)."""

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
    """Polls until gluetun reports healthy (here it flips during the first sleep)
    — gates the post-restart re-verify so we don't test a still-starting tunnel."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="starting")

    def flip_to_healthy(_seconds: float) -> None:
        fake._resolve("gluetun")["State"]["Health"]["Status"] = "healthy"

    assert wait_for_healthy(fake, "gluetun", 60, _logger(), sleep=flip_to_healthy) is True


def test_wait_for_healthy_times_out() -> None:
    """If gluetun never becomes healthy, the wait returns False (→ recovery
    reports FAILED rather than churning dependents into a dead tunnel)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="starting")
    assert wait_for_healthy(fake, "gluetun", 10, _logger(), sleep=lambda _s: None) is False


# ----- wait_for_dns -----


def test_wait_for_dns_succeeds() -> None:
    """DNS+connectivity probe passing returns True (DNS settled after restart)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(0, "")
    assert wait_for_dns(fake, "gluetun", 30, _logger(), sleep=lambda _s: None) is True


def test_wait_for_dns_times_out_but_proceeds() -> None:
    """DNS not settling is non-fatal: we warn and proceed (False) rather than
    block recovery forever — DNS often lags healthy by a beat."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(1, "")
    assert wait_for_dns(fake, "gluetun", 4, _logger(), sleep=lambda _s: None) is False


# ----- restart_gluetun -----


def test_restart_gluetun_success() -> None:
    """Happy path: restart issued and gluetun comes back healthy → True."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = lambda name, cmd: ExecResult(0, "")
    cfg = Config()
    assert restart_gluetun(fake, cfg, _logger(), sleep=lambda _s: None) is True
    assert fake.restarted == ["gluetun"]


def test_restart_gluetun_fails_if_never_healthy() -> None:
    """Restart that doesn't recover → False, so the loop doesn't proceed to
    dependents on a still-broken tunnel (Tenet 5)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="unhealthy")
    cfg = Config(healthy_wait_timeout=10)
    assert restart_gluetun(fake, cfg, _logger(), sleep=lambda _s: None) is False


# ----- verify_dependent -----


def test_verify_dependent_live() -> None:
    """Post-remediation verify passes when the dependent is running + non-lo."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = _live_exec
    assert verify_dependent(fake, "dep") is True


def test_verify_dependent_stranded_fails() -> None:
    """Verify fails if the dependent is still loopback-only after remediation →
    the action didn't stick → FAILED."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(0, "lo\n")
    assert verify_dependent(fake, "dep") is False


# ----- remediate_dependent matrix -----


def test_remediate_restart_path() -> None:
    """Same-id strand → docker restart (cheap rejoin), not a recreate."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = _live_exec
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert fake.restarted == ["dep"]
    assert fake.created == []  # restart, not recreate


def test_remediate_recreate_path() -> None:
    """Moved-id strand → recreate (rm WITHOUT -v, then create), not restart —
    the volume-preserving #20 B2 path."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")  # gluetun id moved
    fake.on_exec = _live_exec
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert fake.removed == [("dep.gm-recreate-old", False)]  # parked old, volumes kept
    assert len(fake.created) == 1
    assert fake.restarted == []  # recreate, not restart


def test_remediate_recreate_disabled_is_failed() -> None:
    """AUTO_RECREATE=0 + a moved-id strand → FAILED, and crucially nothing is
    removed/destroyed (Tenet 1 — don't act when the operator opted out)."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")
    fake.on_exec = _live_exec
    cfg = Config(auto_recreate=False)
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, cfg, _logger(), sleep=lambda _s: None)
    assert ok is False
    assert fake.removed == []  # nothing destroyed when disabled


def test_remediate_try_restart_escalates_to_recreate() -> None:
    """Name-form target: try restart first, and if that fails (dead netns),
    escalate to recreate — ADR-0004's safe path for the untested name form."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode="container:gluetun")  # name form -> TRY_RESTART
    fake.on_exec = _live_exec

    def failing_restart(_name: str) -> None:
        raise RuntimeError("cannot restart into vanished netns")

    fake.restart = failing_restart  # type: ignore[method-assign]
    ok = remediate_dependent(fake, "dep", GLUETUN_ID, Config(), _logger(), sleep=lambda _s: None)
    assert ok is True
    assert len(fake.created) == 1  # escalated to recreate
