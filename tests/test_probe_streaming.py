"""Streaming probe logs with an (n/total) progress marker.

The gateway and dependent phases fan probes out concurrently and now consume the
results as a *stream* (completion order), logging each the moment it lands with an
``(n/total)`` counter — so a slow site is visibly the one still outstanding instead
of hiding the fast results behind one batched burst at gather-end. These tests lock
that every probe is logged exactly once and the counter covers the full ``1..total``.
"""

from __future__ import annotations

import io
import random
import re
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
SITES = [f"https://site{i}.example" for i in range(5)]


def _mon(tmp_path: Path, fake: FakeDockerClient) -> tuple[Monitor, io.StringIO]:
    conf = tmp_path / "sites.conf"
    conf.write_text("\n".join(SITES) + "\n")
    stream = io.StringIO()
    mon = Monitor(
        fake, Config(config_file=str(conf), gluetun_container="gluetun"),
        Logger(log_file=None, level="DEBUG", stream=stream),
        rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None),
    )
    return mon, stream


def test_every_gateway_probe_is_logged_with_a_counter(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda n, c: (
        ExecResult(0, "eth0\nlo\ntun0\n") if c[:2] == ["ls", "/sys/class/net"]
        else ExecResult(0, "  HTTP/1.1 200 OK\n")
    )
    mon, stream = _mon(tmp_path, fake)
    mon.run_once()
    out = stream.getvalue()

    total = len(SITES)
    # Every gateway probe line carries "(n/total)"; the indices seen must be exactly 1..total.
    indices = {int(m) for m in re.findall(rf"\[gateway:gluetun\] \((\d+)/{total}\) reach ", out)}
    assert indices == set(range(1, total + 1)), out
    # ...and one line per site (no probe dropped or double-logged).
    assert len(re.findall(rf"\[gateway:gluetun\] \(\d+/{total}\) reach ", out)) == total, out


def test_single_site_uses_worker_free_path_and_still_counts(tmp_path: Path) -> None:
    """With one site the pool short-circuits to the in-line path — the counter
    ((1/1)) must still be emitted so the format is uniform regardless of pool size."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://only.example\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda n, c: (
        ExecResult(0, "eth0\nlo\ntun0\n") if c[:2] == ["ls", "/sys/class/net"]
        else ExecResult(0, "  HTTP/1.1 200 OK\n")
    )
    stream = io.StringIO()
    mon = Monitor(
        fake, Config(config_file=str(conf), gluetun_container="gluetun"),
        Logger(log_file=None, level="DEBUG", stream=stream),
        rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None),
    )
    mon.run_once()
    assert "[gateway:gluetun] (1/1) reach ok: https://only.example" in stream.getvalue()
