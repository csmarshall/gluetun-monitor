"""DEPENDENT_VIABILITY opt-out (A3) and MAX_JITTER_MS opt-in jitter (A4).

Why: ADR-0006 promised both knobs. DEPENDENT_VIABILITY lets an operator drop the
L7 (DNS/connectivity) probe and rely on the L3 interface/strand check alone;
MAX_JITTER_MS exists for load-spreading but defaults to 0 so the simple path (the
concurrency cap) is the default. These tests pin those behaviors.
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


def _mon(fake: FakeDockerClient, sleep=lambda _s: None, **cfg: object) -> Monitor:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, Config(**cfg), logger, rng=random.Random(0), sleep=sleep)


def test_viability_disabled_skips_l7_fetch_but_keeps_interface_check() -> None:
    """With DEPENDENT_VIABILITY off, a live dependent is judged healthy on the
    interface check alone — no wget/URL fetch is issued (L3 only, no L7)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    wget_calls: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE
        wget_calls.append(cmd)
        return ExecResult(4, "")  # would FAIL if a viability probe were issued

    fake.on_exec = handler
    mon = _mon(fake, dependent_viability=False)
    probe = mon._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert probe.viability_ok is None
    assert "disabled" in probe.reason
    assert wget_calls == []  # the L7 probe was skipped


def test_viability_enabled_by_default_does_fetch() -> None:
    """Default behavior is unchanged: the L7 probe runs."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(0, "eth0\nlo\n" if cmd[0] == "ls" else "")
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert probe.viability_ok is True


def test_no_jitter_by_default() -> None:
    """MAX_JITTER_MS defaults to 0 → no jitter sleep is taken."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(0, "eth0\nlo\n" if cmd[0] == "ls" else "")
    slept: list[float] = []
    mon = _mon(fake, sleep=lambda s: slept.append(s))
    mon._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert slept == []


def test_jitter_sleeps_within_window_when_enabled() -> None:
    """With MAX_JITTER_MS set, a per-dispatch jitter in [0, window] seconds is
    taken before probing — present but opt-in."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda name, cmd: ExecResult(0, "eth0\nlo\n" if cmd[0] == "ls" else "")
    slept: list[float] = []
    mon = _mon(fake, max_jitter_ms=100, sleep=lambda s: slept.append(s))
    mon._probe_dependent("dep", GLUETUN_ID, ["https://x"], [])
    assert len(slept) == 1
    assert 0.0 <= slept[0] <= 0.1  # 100 ms window


def test_env_reads_new_knobs(monkeypatch) -> None:
    monkeypatch.setenv("DEPENDENT_VIABILITY", "0")
    monkeypatch.setenv("MAX_JITTER_MS", "250")
    c = Config.from_env()
    assert c.dependent_viability is False
    assert c.max_jitter_ms == 250
