"""DEPENDENT_VIABILITY_SAMPLES — how many sites each dependent tests per loop.

Why: default 1 (one shuffled site — optimal, since dependents share gluetun's
netns so only DNS differs); N samples N distinct sites; -1 samples all. Viable if
ANY sampled site resolves; not-viable only if ALL sampled resolvable sites fail
DNS. The dial is for completeness; this pins the count + the combine semantics.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
POOL = ["https://a.example", "https://b.example", "https://c.example", "https://d.example"]


def _mon(fake: FakeDockerClient, **cfg: object) -> Monitor:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, Config(**cfg), logger, rng=random.Random(0),
                   sleep=lambda _s: None, stats=SiteStatsStore(None))


def _count_wget(fake: FakeDockerClient) -> list[int]:
    """Track how many wget (viability) execs happen per probe call."""
    calls: list[str] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE
        if cmd and cmd[0] == "wget":
            calls.append(cmd[-1])
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    return calls


def test_default_samples_one_site() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls = _count_wget(fake)
    mon = _mon(fake)  # default samples=1
    mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert len(calls) == 1


def test_n_samples_n_distinct_sites() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls = _count_wget(fake)
    mon = _mon(fake, dependent_viability_samples=3)
    mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert len(calls) == 3
    assert len(set(calls)) == 3  # distinct


def test_minus_one_samples_all() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls = _count_wget(fake)
    mon = _mon(fake, dependent_viability_samples=-1)
    mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert len(calls) == len(POOL)


def test_samples_capped_at_pool_size() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls = _count_wget(fake)
    mon = _mon(fake, dependent_viability_samples=99)  # more than pool
    mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert len(calls) == len(POOL)


def test_viable_if_any_sample_resolves() -> None:
    """Sample 3; one resolves, two DNS-fail -> still viable (DNS works)."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    ok_host = "a.example"

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if ok_host in cmd[-1]:
            return ExecResult(0, "  HTTP/1.1 200 OK\n")
        return ExecResult(1, "wget: bad address\n")  # DNS fail

    fake.on_exec = handler
    mon = _mon(fake, dependent_viability_samples=-1)  # test all -> a.example resolves
    probe = mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert probe.viability_ok is True


def test_not_viable_only_if_all_samples_fail_dns() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        return ExecResult(1, "wget: bad address\n")  # all DNS-fail

    fake.on_exec = handler
    mon = _mon(fake, dependent_viability_samples=-1)
    probe = mon._probe_dependent("dep", GLUETUN_ID, POOL, [])
    assert probe.viability_ok is False
