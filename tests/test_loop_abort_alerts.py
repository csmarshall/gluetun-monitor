"""Aborted monitoring loops must not lie to the operator (#74).

Why: AlertState diffs "reported this loop" against the active set, and the
monitor flushes in ``finally`` — so before #74, ANY loop that bailed early
(gluetun down, recovery failed) or died on an exception flushed an empty report
set and sent "resolved: …" for every active alert, precisely during the outage.
And gluetun being flat-out down — the most severe observable condition — never
notified at all. These tests pin both fixes end to end through Monitor.run_once.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ContainerInfo, ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64


class ExplodingClient(FakeDockerClient):
    """A fake whose inspect() can be switched to raise — the docker socket
    dying mid-loop (DockerPyClient only swallows NotFound; APIErrors propagate).
    """

    def __init__(self) -> None:
        super().__init__()
        self.explode = False

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        if self.explode:
            raise RuntimeError("docker socket died")
        return super().inspect(name_or_id)


def _monitor(fake: FakeDockerClient, conf: str) -> tuple[Monitor, FakeNotifier, io.StringIO]:
    fake.on_exec = lambda n, c: ExecResult(0, "  HTTP/1.1 200 OK\n")
    notifier = FakeNotifier()
    stream = io.StringIO()
    cfg = Config(config_file=conf, gluetun_container="gluetun")
    mon = Monitor(
        fake, cfg, Logger(log_file=None, level="DEBUG", stream=stream),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )
    return mon, notifier, stream


def _seed_active_alert(mon: Monitor, key: str) -> None:
    """Make ``key`` an already-announced active alert (as if a dependent failed
    remediation on an earlier loop)."""
    mon.alerts.begin_loop()
    mon.alerts.report(key, "attention", f"dependent unhealthy: {key}", "b")
    assert len(mon.alerts.events()) == 1  # announced


def _conf(tmp_path: Path) -> str:
    p = tmp_path / "sites.conf"
    p.write_text("https://a.example\n")
    return str(p)


def test_gluetun_down_notifies_and_resolves_on_return(tmp_path: Path) -> None:
    """gluetun stopped at runtime: exactly one 'gluetun-down' attention alert
    (edge-triggered, not per-loop), and a resolve when it comes back."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, running=False)
    mon, notifier, _ = _monitor(fake, _conf(tmp_path))

    mon.run_once()
    mon.run_once()  # still down: edge-triggered → no re-announce
    down = [e for e in notifier.events if e.key == "gluetun-down"]
    assert len(down) == 1 and down[0].tier == "attention"

    fake.restart("gluetun")  # it returns healthy
    mon.run_once()
    assert "resolve:gluetun-down" in notifier.event_keys()


def test_gluetun_down_loop_holds_other_active_alerts(tmp_path: Path) -> None:
    """The early return must not flush-resolve alerts it never evaluated —
    pre-fix this sent 'resolved: dependent unhealthy' during the outage."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, running=False)
    mon, notifier, _ = _monitor(fake, _conf(tmp_path))
    _seed_active_alert(mon, "dependent-unhealthy:app")

    mon.run_once()  # gluetun down → incomplete loop
    assert not [k for k in notifier.event_keys() if k.startswith("resolve:")]
    assert mon.alerts.active_count() == 2  # dependent alert + gluetun-down

    fake.restart("gluetun")
    mon.run_once()  # complete loop: dependent healthy again → genuine resolve
    assert "resolve:dependent-unhealthy:app" in notifier.event_keys()


def test_exception_mid_loop_holds_active_alerts(tmp_path: Path) -> None:
    """A loop that dies on a propagating Docker error (socket gone) must hold
    active alerts, not resolve them — and still re-raise for run()'s catch-all."""
    fake = ExplodingClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    mon, notifier, _ = _monitor(fake, _conf(tmp_path))
    _seed_active_alert(mon, "dependent-unhealthy:app")

    fake.explode = True
    with pytest.raises(RuntimeError):
        mon.run_once()
    assert not [k for k in notifier.event_keys() if k.startswith("resolve:")]
    assert mon.alerts.active_count() == 1  # held

    fake.explode = False
    mon.run_once()  # next complete loop: now the resolve is genuine
    assert "resolve:dependent-unhealthy:app" in notifier.event_keys()


def test_unprobeable_dependent_holds_its_active_alert(tmp_path: Path) -> None:
    """A dependent the loop can't evaluate (empty site pool → viability None) must
    HOLD its active alert, not false-resolve it mid-incident (review finding H3)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.add_container("app", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda n, c: (
        ExecResult(0, "eth0\nlo\ntun0\n") if c[:2] == ["ls", "/sys/class/net"]
        else ExecResult(0, "  HTTP/1.1 200 OK\n"))
    notifier = FakeNotifier()
    cfg = Config(config_file=_conf(tmp_path), gluetun_container="gluetun",
                 dependent_containers="app")
    mon = Monitor(fake, cfg, Logger(log_file=None, level="DEBUG", stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None,
                  stats=SiteStatsStore(None), notifier=notifier)
    _seed_active_alert(mon, "dependent-wedged:app")

    mon.alerts.begin_loop()
    mon.run_dependent_phase(GLUETUN_ID, [])  # no sites -> app un-probeable this loop
    keys = [e.key for e in mon.alerts.events()]
    assert "resolve:dependent-wedged:app" not in keys  # not false-resolved
    assert mon.alerts.is_active("dependent-wedged:app")  # still active


def test_probe_dependent_survives_a_transient_inspect_error(tmp_path: Path) -> None:
    """One dependent's Docker inspect error must not propagate out of the fan-out
    and abort every other dependent's remediation (review finding). It returns an
    un-evaluated probe (running so it isn't churned) instead of raising."""
    from gluetun_monitor.state import InterfaceStatus

    class Boom(FakeDockerClient):
        def inspect(self, name_or_id: str):  # type: ignore[no-untyped-def]
            if name_or_id == "distroless":
                raise RuntimeError("APIError: socket blip")
            return super().inspect(name_or_id)

    fake = Boom()
    fake.add_container("distroless", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = lambda n, c: ExecResult(1, "")  # no shell -> UNKNOWN -> inspect path
    mon = Monitor(fake, Config(config_file=_conf(tmp_path), gluetun_container="gluetun"),
                  Logger(log_file=None, stream=io.StringIO()), rng=random.Random(0),
                  sleep=lambda _s: None, stats=SiteStatsStore(None))
    probe = mon._probe_dependent("distroless", GLUETUN_ID, [], [])  # must NOT raise
    assert probe.status is InterfaceStatus.UNKNOWN
    assert probe.running is True  # conservative: not remediated as "not running"
