"""Interface (L3 route) check: capture the interface set + surface it in DEBUG.

Why: "can a dependent route out" is the eth0/tun0-vs-lo-only question, and an
operator wants to *see* it each loop — not just the DNS result. list_interfaces
exposes the actual set; classify_interfaces turns it into the verdict; and the
dependent-phase DEBUG line now shows both.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.dependents import classify_interfaces, list_interfaces
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore
from gluetun_monitor.state import InterfaceStatus

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_list_interfaces_parses_the_set() -> None:
    fake = FakeDockerClient()
    fake.on_exec = lambda n, c: ExecResult(0, "eth0\nlo\ntun0\n")
    assert list_interfaces(fake, "dep") == {"eth0", "lo", "tun0"}


def test_list_interfaces_none_on_failure() -> None:
    fake = FakeDockerClient()
    fake.on_exec = lambda n, c: ExecResult(1, "")  # no shell / error
    assert list_interfaces(fake, "dep") is None


def test_classify_interfaces() -> None:
    assert classify_interfaces({"eth0", "lo"}) is InterfaceStatus.LIVE
    assert classify_interfaces({"lo"}) is InterfaceStatus.STRANDED
    assert classify_interfaces(None) is InterfaceStatus.UNKNOWN
    assert classify_interfaces(set()) is InterfaceStatus.UNKNOWN


def _mon(fake: FakeDockerClient, tmp_path: Path) -> tuple[Monitor, io.StringIO]:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    stream = io.StringIO()
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return (
        Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None,
                stats=SiteStatsStore(None)),
        stream,
    )


def test_live_dependent_logs_its_interfaces(tmp_path: Path) -> None:
    """A healthy dependent's DEBUG line shows the interface verdict AND the actual
    interfaces (the route check), alongside the DNS result."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\ntun0\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    mon, stream = _mon(fake, tmp_path)
    mon.run_once()
    out = stream.getvalue()
    # Two ordered lines: the L3 link check first, then the reach (DNS/connect) test.
    assert "[dependent:dep] (1/1) link live: eth0,lo,tun0" in out
    assert "[dependent:dep] reach " in out
    iface_at = out.index("[dependent:dep] (1/1) link ")
    reach_at = out.index("[dependent:dep] reach ")
    assert iface_at < reach_at  # path validated BEFORE the DNS/connect test


def test_stranded_dependent_logs_loopback_only(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda n, c: (
        ExecResult(0, "lo\n") if c[:2] == ["ls", "/sys/class/net"] else ExecResult(0, "")
    )
    mon, stream = _mon(fake, tmp_path)
    mon.run_once()
    assert "[dependent:dep] (1/1) link stranded: lo" in stream.getvalue()
