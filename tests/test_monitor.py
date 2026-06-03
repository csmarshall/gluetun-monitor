"""End-to-end state machine via Monitor.run_once against the fake daemon.

Why: these are the integration tests for the ADR-0006 per-loop state machine —
they exercise the real decision flow (gluetun root test → restart/re-verify →
per-dependent strand/viability → restart-vs-recreate) end to end, which the unit
tests only cover piecewise. They're the closest thing to "does the #20 fix
actually work" without a live daemon.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64
NEW_ID = "c" * 64


@pytest.fixture
def sites_file(tmp_path: Path) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\nhttps://cloudflare.com\nhttps://1.1.1.1\n")
    return str(conf)


def _monitor(fake: FakeDockerClient, sites_file: str, **cfg_overrides) -> Monitor:
    cfg = Config(config_file=sites_file, gluetun_container="gluetun", **cfg_overrides)
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None)


def test_gluetun_not_running_does_nothing(sites_file: str) -> None:
    """A stopped gluetun → log + take no action this loop (don't restart a
    gluetun that's deliberately down; the next loop re-checks)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, running=False)
    _monitor(fake, sites_file).run_once()
    assert fake.restarted == []


def test_healthy_no_dependents_is_quiet(sites_file: str) -> None:
    """Healthy gluetun, no dependents → no restarts/recreates: the steady state
    must be completely passive (no churn on a healthy stack)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(0, "")  # all wget pass
    _monitor(fake, sites_file).run_once()
    assert fake.restarted == []
    assert fake.created == []


def test_healthy_dependent_passes_viability(sites_file: str) -> None:
    """A live dependent whose viability probe passes is left alone — a working
    dependent is never touched."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE
        return ExecResult(0, "")  # wget passes

    fake.on_exec = handler
    _monitor(fake, sites_file).run_once()
    assert fake.restarted == []


def test_stranded_dependent_is_restarted(sites_file: str) -> None:
    """The headline #20 case: a loopback-stranded dependent on gluetun's *current*
    id is healed by a restart (not a recreate), and the verify then passes."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            # Stranded until restarted, then live (so the verify passes).
            if "dep" in fake.restarted:
                return ExecResult(0, "eth0\nlo\n")
            return ExecResult(0, "lo\n")
        return ExecResult(0, "")

    fake.on_exec = handler
    _monitor(fake, sites_file).run_once()
    assert fake.restarted == ["dep"]  # same-id strand -> restart
    assert fake.created == []


def test_stranded_dependent_with_moved_gluetun_id_is_recreated(sites_file: str) -> None:
    """The Watchtower case: gluetun was recreated (new id), so the strand is
    healed by a volume-preserving recreate (rm without -v), not a restart."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    # Dependent still points at the OLD gluetun id -> recreate, not restart.
    # It predates the monitor and its NetworkMode no longer matches the current
    # gluetun id, so it's named explicitly (the documented path for this case).
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            if fake.created:  # after recreate
                return ExecResult(0, "eth0\nlo\n")
            return ExecResult(0, "lo\n")
        return ExecResult(0, "")

    fake.on_exec = handler
    _monitor(fake, sites_file, dependent_containers="dep").run_once()
    assert len(fake.created) == 1
    assert fake.removed == [("dep", False)]
    assert fake.restarted == []


def test_remembered_dependent_recreated_after_live_gluetun_recreate(sites_file: str) -> None:
    """A dependent discovered while healthy is still tracked (and recreated) after
    gluetun is recreated under it — the remembered-set path, no explicit list."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    state = {"gluetun_recreated": False}

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            if state["gluetun_recreated"] and not fake.created:
                return ExecResult(0, "lo\n")  # stranded until recreated
            return ExecResult(0, "eth0\nlo\n")
        return ExecResult(0, "")

    fake.on_exec = handler
    mon = _monitor(fake, sites_file)

    mon.run_once()  # loop 1: dep healthy, remembered
    assert fake.created == []
    assert "dep" in mon._known_dependents

    # Gluetun is recreated out of band: new id, dep left pointing at the old one.
    fake.remove("gluetun", volumes=False)
    fake.add_container("gluetun", id=NEW_ID)
    state["gluetun_recreated"] = True

    mon.run_once()  # loop 2: dep no longer matches current id, but is remembered
    assert len(fake.created) == 1  # recreated onto the new gluetun id
    assert fake.removed[-1] == ("dep", False)


def test_viability_failure_accumulates_to_threshold(sites_file: str) -> None:
    """A live dependent failing its viability probe isn't remediated on the first
    failure — it must reach DEPENDENT_CONTAINER_FAILURES consecutive loops first
    (Tenet 8, under-react to noise)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # always LIVE
        if name == "gluetun":
            return ExecResult(0, "")  # gluetun root test passes
        return ExecResult(1, "wget: bad address 'site'")  # dependent DNS failure

    fake.on_exec = handler
    mon = _monitor(fake, sites_file, dependent_container_failures=2)

    mon.run_once()  # fail 1/2 — no remediation yet
    assert fake.restarted == []
    assert mon.dependent_failures.get("dep") == 1

    mon.run_once()  # fail 2/2 — remediate
    assert fake.restarted == ["dep"]


def test_gluetun_failure_triggers_restart_then_reverify(sites_file: str) -> None:
    """gluetun's own site failures must breach FAIL_THRESHOLD before it's
    restarted; the ordered-recovery path then restarts gluetun and re-verifies
    (ADR-0003) — no single-blip restarts."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        # gluetun site wget fails until gluetun is restarted, then passes.
        if name == "gluetun" and "gluetun" not in fake.restarted:
            return ExecResult(4, "")
        return ExecResult(0, "")

    fake.on_exec = handler
    mon = _monitor(fake, sites_file, fail_threshold=2)

    mon.run_once()  # sites fail 1/2 — below threshold, no restart
    assert fake.restarted == []

    mon.run_once()  # sites fail 2/2 — breach -> restart gluetun, then re-verify passes
    assert fake.restarted == ["gluetun"]
