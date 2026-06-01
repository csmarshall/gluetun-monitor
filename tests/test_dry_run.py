"""DRY_RUN observe-only mode: detect + log intended actions, never mutate.

Why: a watchdog that restarts/recreates containers needs a way to be soak-tested
against a real stack — alongside an already-active monitor — without two actors
fighting. DRY_RUN runs all read-only detection (inspect / ls / wget) so its
*decisions* are visible, but replaces every mutating action with a
"[DRY-RUN] would ..." log line. These tests pin that nothing is restarted,
removed, recreated, or started while observing is on.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64


def _sites(tmp_path: Path) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    return str(conf)


def _mon(fake: FakeDockerClient, sites: str, **cfg) -> tuple[Monitor, io.StringIO]:
    stream = io.StringIO()
    config = Config(config_file=sites, gluetun_container="gluetun", dry_run=True, **cfg)
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return Monitor(fake, config, logger, rng=random.Random(0), sleep=lambda _s: None), stream


def _assert_no_mutations(fake: FakeDockerClient) -> None:
    assert fake.restarted == []
    assert fake.removed == []
    assert fake.created == []
    assert fake.started == []


def test_dry_run_stranded_dependent_is_not_restarted(tmp_path: Path) -> None:
    """A stranded dependent that would normally be restarted is only logged."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "lo\n")  # stranded
        return ExecResult(0, "")

    fake.on_exec = handler
    mon, stream = _mon(fake, _sites(tmp_path))
    mon.run_once()
    _assert_no_mutations(fake)
    out = stream.getvalue()
    assert "[DRY-RUN] would remediate dep" in out
    assert "action=RESTART" in out


def test_dry_run_moved_id_would_recreate_but_does_not(tmp_path: Path) -> None:
    """A moved-id strand reports action=RECREATE but nothing is removed/created."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "lo\n")  # stranded
        return ExecResult(0, "")

    fake.on_exec = handler
    mon, stream = _mon(fake, _sites(tmp_path), dependent_containers="dep")
    mon.run_once()
    _assert_no_mutations(fake)
    assert "action=RECREATE" in stream.getvalue()


def test_dry_run_gluetun_failure_is_not_restarted(tmp_path: Path) -> None:
    """When gluetun's sites breach threshold, dry-run logs the would-restart and
    still probes dependents, but never restarts gluetun."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        return ExecResult(4, "")  # every site probe fails

    fake.on_exec = handler
    mon, stream = _mon(fake, _sites(tmp_path), fail_threshold=1)
    mon.run_once()
    _assert_no_mutations(fake)
    assert "[DRY-RUN] would restart gluetun" in stream.getvalue()


def test_dry_run_still_counts_failures(tmp_path: Path) -> None:
    """Detection/counting is real in dry-run — the failure counter still climbs,
    so the observed decision matches what a live run would do."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if name == "gluetun":
            return ExecResult(0, "")  # gluetun root test passes
        return ExecResult(4, "")  # dependent viability fails

    fake.on_exec = handler
    mon, _ = _mon(fake, _sites(tmp_path), dependent_container_failures=2)
    mon.run_once()
    assert mon.dependent_failures.get("dep") == 1
    mon.run_once()
    assert mon.dependent_failures.get("dep") == 2  # counts for real; just no action
    _assert_no_mutations(fake)


def test_dry_run_off_by_default() -> None:
    assert Config().dry_run is False
