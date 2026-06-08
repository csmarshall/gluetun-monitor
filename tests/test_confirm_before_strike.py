"""Confirm-before-strike: a single dependent DNS-BROKEN is re-tested against a
second name before it counts (#61).

Why: with one shuffled viability name per loop, a single DNS-BROKEN can't tell a
dead *domain* (a flaky site in the pool) from a broken *resolver* (the fault we
remediate). When several pool names fail at once — the observed ~05:00 window —
consecutive bad draws could remediate a healthy dependent on the back of dead
sites. So on a lone BROKEN we draw one more *different* name and strike only if it
also fails. These tests pin: a dead domain alone never strikes; a genuinely broken
resolver still does; the confirm is bounded (one extra probe) and skipped when
eager-N already tested several names or the pool has nothing to confirm against.
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
BAD = ExecResult(1, "wget: bad address 'x'\n")  # DNS-BROKEN
OK = ExecResult(0, "  HTTP/1.1 200 OK\n")  # resolved + responded


def _mon(fake: FakeDockerClient, **cfg: object) -> Monitor:
    config = Config(config_file="/dev/null", gluetun_container="gluetun", **cfg)
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, config, logger, rng=random.Random(0), sleep=lambda _s: None)


def _live_dep(*wget_results: ExecResult) -> tuple[FakeDockerClient, list[str]]:
    """A LIVE dependent whose successive viability wget probes return ``wget_results``
    in order (then the last result repeats). Records the probed hosts so a test can
    assert how many probes ran."""
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    probes: list[str] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE (non-loopback present)
        if cmd and cmd[0] == "wget":
            i = len(probes)
            probes.append(cmd[-1])
            return wget_results[min(i, len(wget_results) - 1)]
        return ExecResult(0, "")

    fake.on_exec = handler
    return fake, probes


def test_dead_domain_alone_does_not_strike() -> None:
    """First name DNS-fails but the confirm name resolves → viable (a dead site,
    not the resolver). Exactly two probes run (the sample + one confirm)."""
    fake, probes = _live_dep(BAD, OK)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://a", "https://b"], [])
    assert probe.viability_ok is True
    assert len(probes) == 2  # confirmed, and bounded to one extra
    assert "dead site" in probe.reason


def test_broken_resolver_still_strikes() -> None:
    """Both the sample and the confirm DNS-fail → not viable (genuine resolver
    fault — two independent names couldn't resolve)."""
    fake, probes = _live_dep(BAD, BAD)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://a", "https://b"], [])
    assert probe.viability_ok is False
    assert len(probes) == 2
    assert "resolver broken" in probe.reason


def test_single_resolvable_url_falls_back_to_one_sample() -> None:
    """Nothing to confirm against (one resolvable URL) → keep the single-sample
    verdict and do NOT spend a second probe."""
    fake, probes = _live_dep(BAD)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://only"], [])
    assert probe.viability_ok is False
    assert len(probes) == 1  # no confirm attempted


def test_healthy_first_sample_costs_one_probe() -> None:
    """The common case is untouched: a passing first sample → viable on one probe,
    no confirm."""
    fake, probes = _live_dep(OK)
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://a", "https://b"], [])
    assert probe.viability_ok is True
    assert len(probes) == 1


def test_eager_sampling_is_a_no_op() -> None:
    """With DEPENDENT_VIABILITY_SAMPLES=2 the loop already tested two names; both
    failing → strike with no extra confirm probe (no-op there)."""
    fake, probes = _live_dep(BAD, BAD)
    mon = _mon(fake, dependent_viability_samples=2)
    probe = mon._probe_dependent("dep", GLUETUN_ID, ["https://a", "https://b"], [])
    assert probe.viability_ok is False
    assert len(probes) == 2  # the two eager samples, no third confirm
    assert "sampled failed" in probe.reason


def test_dead_domain_never_remediates_across_loops(tmp_path) -> None:
    """End to end: a healthy dependent whose unlucky draw keeps hitting a dead
    domain is never remediated, because each loop's confirm resolves."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://dead.example\nhttps://live.example\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if name == "gluetun":
            return OK  # gluetun root test always passes
        # The dependent: the dead domain fails DNS, the live one resolves.
        return BAD if cmd[-1] == "https://dead.example" else OK

    fake.on_exec = handler
    cfg = Config(config_file=str(conf), gluetun_container="gluetun",
                 dependent_container_failures=2)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None)
    for _ in range(5):
        mon.run_once()
    assert fake.restarted == []  # the confirm absorbs every dead-domain draw
    assert mon.dependent_failures.get("dep") == 0
