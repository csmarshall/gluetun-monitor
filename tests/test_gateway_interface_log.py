"""The gateway logs its own interface/route check before the site curls.

Why: symmetry with the dependent checks + early visibility of tun0 (tunnel up at
L3) vs "sites unreachable". It's observability only — the site tests remain the
authoritative tunnel check (ADR-0001), so this never gates behavior.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_gateway_interface_logged_before_site_tests(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\ntun0\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    stream = io.StringIO()
    mon = Monitor(
        fake, Config(config_file=str(conf), gluetun_container="gluetun"),
        Logger(log_file=None, level="DEBUG", stream=stream),
        rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None),
    )
    mon.run_once()
    out = stream.getvalue()
    assert "[gateway:gluetun] interface check: live [eth0,lo,tun0]" in out
    # ...and it comes before the first site curl line.
    gw_at = out.index("[gateway:gluetun] interface check:")
    site_at = out.index("[gateway:gluetun] site https://www.google.com")
    assert gw_at < site_at
