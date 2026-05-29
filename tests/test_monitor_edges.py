"""Degradation + failure-path branches of the monitor (ADR-0006 degradation).

Why: the happy paths are in test_monitor; these pin the *edges* where it's easy
to do the wrong thing — distroless/IP-only degradation, and the recovery gates
that must NOT touch dependents when gluetun can't be restored (Tenet 5: don't
churn dependents into a dead tunnel).
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
from gluetun_monitor.state import InterfaceStatus

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _mon(fake: FakeDockerClient, sites_file: str, **cfg) -> tuple[Monitor, io.StringIO]:
    stream = io.StringIO()
    config = Config(config_file=sites_file, gluetun_container="gluetun", **cfg)
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return Monitor(fake, config, logger, rng=random.Random(0), sleep=lambda _s: None), stream


def _write(tmp_path: Path, body: str) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text(body)
    return str(conf)


# ----- _probe_dependent degradation -----


def test_probe_unknown_running_is_not_remediated() -> None:
    """A distroless dependent (exec fails → UNKNOWN) on the *current* gluetun id is
    left alone — we don't churn a container we can't prove is broken (Tenet 1)."""
    fake = FakeDockerClient()
    fake.add_container("distroless", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(1, "")  # no shell -> exec fails
    mon, _ = _mon(fake, "/dev/null")
    # Same id as current gluetun -> shares the live netns -> left alone (healthy).
    probe = mon._probe_dependent("distroless", GLUETUN_ID, ["https://x"], [])
    assert probe.status is InterfaceStatus.UNKNOWN
    assert probe.running is True
    assert probe.viability_ok is None  # not tested


def test_probe_ip_only_fallback_uses_ip_pool() -> None:
    """With no resolvable names, the viability probe falls back to an IP literal
    (connectivity-only) instead of skipping — ADR-0006 degradation."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    seen: list[str] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        seen.append(cmd[-1])  # the wget URL
        return ExecResult(0, "")

    fake.on_exec = handler
    mon, _ = _mon(fake, "/dev/null")
    probe = mon._probe_dependent("dep", GLUETUN_ID, [], ["https://1.1.1.1"])
    assert probe.viability_ok is True
    assert seen == ["https://1.1.1.1"]


# ----- run_dependent_phase IP-only WARN -----


def test_ip_only_sites_log_dns_warning(tmp_path: Path) -> None:
    """An IP-literal-only sites set logs a WARN that dependent DNS can't be
    validated — a documented limitation must be surfaced, not silent."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: (
        ExecResult(0, "eth0\nlo\n") if cmd[:2] == ["ls", "/sys/class/net"] else ExecResult(0, "")
    )
    sites = _write(tmp_path, "https://1.1.1.1\n")  # IP literal only
    mon, stream = _mon(fake, sites)
    mon.run_once()
    assert "dependent DNS cannot be validated" in stream.getvalue()


# ----- gluetun recovery failure paths -----


def test_gluetun_restart_failure_skips_dependents(tmp_path: Path) -> None:
    """If gluetun won't come back healthy after its restart, recovery reports
    FAILED and does NOT touch dependents (Tenet 5 — no churn into a dead tunnel)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="unhealthy")  # never becomes healthy
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: (
        ExecResult(0, "eth0\nlo\n") if cmd[:2] == ["ls", "/sys/class/net"] else ExecResult(4, "")
    )
    sites = _write(tmp_path, "https://www.google.com\n")
    mon, stream = _mon(fake, sites, fail_threshold=1, healthy_wait_timeout=10)
    mon.run_once()
    assert fake.restarted == ["gluetun"]  # tried to restart gluetun
    assert "Recovery failed" in stream.getvalue()
    assert fake.created == []  # dependents never touched


def test_gluetun_reverify_still_failing_leaves_dependents_untouched(tmp_path: Path) -> None:
    """gluetun restarts and becomes healthy, but sites still fail on re-verify →
    leave dependents untouched and reset counters (don't act on a tunnel that
    came back unhealthy)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if cmd[0] == "nslookup":
            return ExecResult(0, "")  # DNS wait passes
        # All site probes keep failing, even after the restart.
        if name == "gluetun" and "https://1.1.1.1" in cmd:
            return ExecResult(0, "")  # the dns-stability 1.1.1.1 probe
        return ExecResult(4, "")

    fake.on_exec = handler
    sites = _write(tmp_path, "https://www.google.com\n")
    mon, stream = _mon(fake, sites, fail_threshold=1)
    mon.run_once()
    assert fake.restarted == ["gluetun"]
    assert "still failing after restart" in stream.getvalue()
    assert fake.created == []
    assert "dep" not in fake.restarted


@pytest.mark.parametrize("sites_body", ["", "# only comments\n"])
def test_no_sites_warns_and_takes_no_action(tmp_path: Path, sites_body: str) -> None:
    """V1 contract at runtime: an empty sites set tests nothing for gluetun and
    never triggers a restart (no site can fail). We do NOT guess substitute
    targets — startup validation is what rejects an empty config (Tenet 1)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    sites = _write(tmp_path, sites_body)
    mon, stream = _mon(fake, sites)
    mon.run_once()
    assert "No sites configured" in stream.getvalue()
    assert fake.restarted == []
    assert fake.created == []
