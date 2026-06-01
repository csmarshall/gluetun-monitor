"""Dependent viability fails ONLY on DNS resolution failure (the shared-netns model).

Why: dependents share gluetun's network namespace, so their L3/L4 egress is
gluetun's — already proven by the root test, and a strand is caught upstream by
the interface check. The one fault a per-dependent probe uniquely detects is the
container's own DNS (ADR-0006). So a 404/403, a flaky-site connection refusal, or
a TLS hiccup must NOT count as a viability failure (no spurious dependent
restarts); only "couldn't resolve the name" does. These tests pin that at the
_probe_dependent level.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _mon(fake: FakeDockerClient) -> Monitor:
    cfg = Config(config_file="/dev/null", gluetun_container="gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None)


def _live_dep_returning(probe_output: str, probe_exit: int) -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE
        return ExecResult(probe_exit, probe_output)

    fake.on_exec = handler
    return fake


def test_busybox_404_is_viable() -> None:
    """busybox 404 (exit 1, but site responded) → viable (DNS resolved)."""
    fake = _live_dep_returning("  HTTP/1.1 404 Not Found\n", 1)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert probe.viability_ok is True


def test_connection_refused_is_viable() -> None:
    """A flaky site refusing the connection (DNS resolved) → viable; it's the
    remote's problem, not the dependent's (shared netns)."""
    fake = _live_dep_returning("wget: can't connect: Connection refused\n", 1)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert probe.viability_ok is True


def test_dns_failure_is_not_viable() -> None:
    """A real resolver failure → NOT viable (this is the fault we exist to catch)."""
    fake = _live_dep_returning("wget: bad address 'x'\n", 1)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert probe.viability_ok is False


def test_only_dns_failures_accumulate_to_remediation(tmp_path) -> None:
    """End to end: a dependent that only ever gets 404s never trips remediation,
    while one with persistent DNS failures reaches the threshold and is acted on."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")

    # Case 1: dependent always gets a 404 — never remediated.
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def http404(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if name == "gluetun":
            return ExecResult(0, "  HTTP/1.1 200 OK\n")
        return ExecResult(1, "  HTTP/1.1 404 Not Found\n")  # busybox 404

    fake.on_exec = http404
    cfg = Config(config_file=str(conf), gluetun_container="gluetun", dependent_container_failures=2)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None)
    mon.run_once()
    mon.run_once()
    mon.run_once()
    assert fake.restarted == []  # 404s never count
    assert mon.dependent_failures.get("dep") == 0

    # Case 2: dependent's DNS is broken — reaches threshold and is remediated.
    fake2 = FakeDockerClient()
    fake2.add_container("gluetun", id=GLUETUN_ID)
    fake2.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def dnsfail(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if name == "gluetun":
            return ExecResult(0, "  HTTP/1.1 200 OK\n")
        return ExecResult(1, "wget: bad address 'site'\n")  # DNS failure

    fake2.on_exec = dnsfail
    mon2 = Monitor(fake2, cfg, Logger(log_file=None, stream=io.StringIO()),
                   rng=random.Random(0), sleep=lambda _s: None)
    mon2.run_once()
    assert fake2.restarted == []  # 1/2
    mon2.run_once()
    assert fake2.restarted == ["dep"]  # 2/2 -> remediated
