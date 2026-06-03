"""Coverage for monitor decision/observability branches not hit elsewhere.

Interface flap-recovery, the "no test URLs" and all-DNS-unvalidated probe
outcomes, the UNKNOWN-and-not-running remediation trigger, stale-stat pruning,
the announce() discovery lines, and the dangling-orphan name-form skip.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore
from gluetun_monitor.state import InterfaceStatus

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64


def _mon(fake: FakeDockerClient, *, stats: SiteStatsStore | None = None, **cfg: object) -> Monitor:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(fake, Config(**cfg), logger, rng=random.Random(0),
                   sleep=lambda _s: None, stats=stats or SiteStatsStore(None))


def _stream(mon: Monitor) -> io.StringIO:
    stream = mon.log._logger.handlers[0].stream  # type: ignore[attr-defined]
    assert isinstance(stream, io.StringIO)
    return stream


# ----- _probe_dependent: flap-recovery (monitor.py:188-189) -----

def test_stranded_then_recovered_on_recheck() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls = {"n": 0}

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            calls["n"] += 1
            return ExecResult(0, "lo\n") if calls["n"] == 1 else ExecResult(0, "eth0\nlo\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, ["https://x.example"], [])
    assert probe.status is InterfaceStatus.LIVE
    assert probe.reason == "recovered on re-check"


# ----- _probe_dependent: no test URLs (monitor.py:231) -----

def test_live_dependent_with_no_test_urls() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda n, c: ExecResult(0, "eth0\nlo\n")  # LIVE; pools empty below
    probe = _mon(fake)._probe_dependent("dep", GLUETUN_ID, [], [])
    assert probe.viability_ok is None
    assert probe.reason == "no test URLs"


# ----- all sampled sites UNVALIDATED + the loud warn (monitor.py:270-271, 342-346) -----

def test_dns_unvalidated_warns_once_and_relies_on_interface() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")  # LIVE
        return ExecResult(127, "not found")  # wget/getent/ping all absent -> UNVALIDATED

    fake.on_exec = handler
    mon = _mon(fake, dependent_containers="dep")
    mon.run_dependent_phase(GLUETUN_ID, ["https://x.example"])
    mon.run_dependent_phase(GLUETUN_ID, ["https://x.example"])  # second loop: no re-warn
    log = _stream(mon).getvalue()
    assert log.count("using link check only") == 1  # warned once, not per-loop
    assert "reach ?:" in log
    assert fake.restarted == [] and fake.created == []  # never churned (Tenet 1)


# ----- UNKNOWN + not running -> remediate (monitor.py:334-337) -----

def test_unknown_and_not_running_is_remediated() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    # No shell (UNKNOWN) + not running + same id (so inspect fallback won't RECREATE).
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}", running=False)
    fake.on_exec = lambda n, c: ExecResult(1, "")  # distroless
    mon = _mon(fake, dependent_containers="dep")
    mon.run_dependent_phase(GLUETUN_ID, ["https://x.example"])
    assert fake.restarted == ["dep"]  # restarted (same-id strand)


# ----- prune-stale logging (monitor.py:490-493) -----

def test_save_stats_logs_pruned_sites() -> None:
    clock = {"t": 1_000_000.0}
    store = SiteStatsStore(None, clock=lambda: clock["t"])
    store.record_poll("https://old.example", ok=True, duration_ms=5, reason="")
    clock["t"] += 100 * 86400  # 100 days later -> beyond 90d retention
    fake = FakeDockerClient()
    mon = _mon(fake, stats=store, stats_retention_days=90)
    mon._save_stats()
    assert "Pruned 1 stale site" in _stream(mon).getvalue()
    assert "https://old.example" not in store.sites


# ----- announce() discovery lines (monitor.py:505, 509) -----

def test_announce_auto_discovery_lists_dependents() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon = _mon(fake, dependent_containers="auto")
    mon.announce()
    assert "auto-discovery): dep" in _stream(mon).getvalue()


def test_announce_manual_lists_dependents() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon = _mon(fake, dependent_containers="dep")
    mon.announce()
    assert "(manual): dep" in _stream(mon).getvalue()


# ----- dangling-orphan name-form skip (monitor.py:542-546) -----

def test_orphan_warn_skips_nameform_target() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    # Running container whose netns target is a *name* (resolves normally) — not a
    # dangling id, so no orphan warning.
    fake.add_container("buddy", network_mode="container:gluetun")
    mon = _mon(fake, dependent_containers="auto")
    mon._warn_dangling_orphans()
    assert "no longer exists" not in _stream(mon).getvalue()
