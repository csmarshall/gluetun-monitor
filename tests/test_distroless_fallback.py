"""A1: inspect-based strand detection for non-exec'able dependents (ADR-0004).

A distroless/scratch dependent has no shell, so ``ls /sys/class/net`` fails and
the interface check is UNKNOWN. The monitor then falls back to comparing the
dependent's NetworkMode target to gluetun's current id, and acts only on the
unambiguous moved-id verdict — closing the #20 blind spot for shell-less images.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.state import InterfaceStatus

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64


def _mon(fake: FakeDockerClient, **cfg: object) -> Monitor:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    config = Config(**cfg)
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, config, logger, rng=random.Random(0), sleep=lambda _s: None)


def _no_shell(name: str, cmd: list[str]) -> ExecResult:
    """Every exec fails — simulates a distroless image with no shell/wget/ls."""
    return ExecResult(1, "")


def test_distroless_moved_id_is_flagged_stranded() -> None:
    fake = FakeDockerClient()
    fake.add_container("distroless", network_mode=f"container:{OLD_ID}")  # gluetun moved
    fake.on_exec = _no_shell
    probe = _mon(fake)._probe_dependent("distroless", GLUETUN_ID, ["https://x"], [])
    # Interface check was UNKNOWN, but the inspect fallback re-classifies it.
    assert probe.status is InterfaceStatus.STRANDED
    assert "inspect" in probe.reason


def test_distroless_same_id_is_left_alone() -> None:
    fake = FakeDockerClient()
    fake.add_container("distroless", network_mode=f"container:{GLUETUN_ID}")  # current id
    fake.on_exec = _no_shell
    probe = _mon(fake)._probe_dependent("distroless", GLUETUN_ID, ["https://x"], [])
    assert probe.status is InterfaceStatus.UNKNOWN  # not churned (Tenet 2)
    assert probe.running is True


def test_distroless_nameform_target_is_left_alone() -> None:
    fake = FakeDockerClient()
    fake.add_container("distroless", network_mode="container:gluetun")  # name form
    fake.on_exec = _no_shell
    probe = _mon(fake)._probe_dependent("distroless", GLUETUN_ID, ["https://x"], [])
    assert probe.status is InterfaceStatus.UNKNOWN  # can't prove a strand -> leave it


def test_distroless_moved_id_is_recreated_end_to_end(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("distroless", network_mode=f"container:{OLD_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        # gluetun's own site probe passes; the distroless dep has no shell.
        if name == "gluetun" and cmd and cmd[0] == "wget":
            return ExecResult(0, "")
        return ExecResult(1, "")

    fake.on_exec = handler
    mon = _mon(fake, config_file=str(conf), dependent_containers="distroless")
    mon.run_once()
    assert len(fake.created) == 1  # recreated onto the current gluetun id
    assert fake.removed == [("distroless", False)]
