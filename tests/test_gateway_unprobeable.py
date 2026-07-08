"""#137: a gateway probe that never RAN must never be read as a connectivity failure.

When `docker exec` into gluetun fails — no EXEC permission on the socket proxy, the
proxy is down, the container is gone, or it ships no `wget` — the probe tells us
*nothing* about the tunnel. Counting it as a site failure made the monitor restart
gluetun every ~2 loops over a tool it could not invoke, churning every dependent
behind it, forever. Restarting cannot restore an EXEC permission (Tenets 1 and 7).

The dependent path always got this right (`InterfaceStatus.UNKNOWN` → unevaluated →
left alone); these tests pin the same discipline onto the gateway path, including the
subtler trap: an unprobeable *post-restart re-verify* must not be reported as
"recovered" — an empty breach list there is absence of evidence, not proof of health.
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

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64
A = "https://a.example"
B = "https://b.example"


def _denied(*_a: object, **_k: object) -> ExecResult:
    raise RuntimeError("403 Client Error: Forbidden (socket proxy EXEC=0)")


def _monitor(tmp_path: Path, notifier: FakeNotifier) -> tuple[Monitor, FakeDockerClient]:
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{A}\n{B}\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    mon = Monitor(
        fake,
        Config(config_file=str(conf), gluetun_container="gluetun",
               fail_threshold=2, dns_wait_timeout=0, advisory_min_restarts=999),
        Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )
    return mon, fake


def test_probe_site_flags_exec_failure() -> None:
    """The signal itself: an exec that never ran is marked, not disguised as a failure."""
    from gluetun_monitor.connectivity import probe_site

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = _denied
    result = probe_site(fake, "gluetun", A, timeout=1, tries=1)
    assert result.exec_failed is True
    assert result.ok is False
    assert result.exit_code == -1


def test_unprobeable_gateway_never_restarts_gluetun(tmp_path: Path) -> None:
    """The core #137 regression (RED pre-fix): exec denied every loop → the monitor
    restarted gluetun every ~2 loops. It must now restart NOTHING and say why."""
    notifier = FakeNotifier()
    mon, fake = _monitor(tmp_path, notifier)
    fake.on_exec = _denied

    for _ in range(4):
        mon.run_once()

    keys = notifier.event_keys()
    assert fake.restarted == [], "must never restart the tunnel over a probe that can't run"
    assert (fake.removed, fake.created) == ([], []), "dependents must be left alone"
    assert "gluetun-unprobeable" in keys, keys
    assert "gluetun-recovered" not in keys, keys
    assert keys.count("gluetun-unprobeable") == 1, "edge-triggered: announced once"


def test_unprobeable_reverify_does_not_claim_recovery(tmp_path: Path) -> None:
    """The subtler trap: a REAL breach restarts gluetun, but the post-restart re-verify
    can't run. An empty breach list there is absence of evidence — claiming
    'recovered' would be exactly the fake-green Tenet 7 forbids."""
    notifier = FakeNotifier()
    mon, fake = _monitor(tmp_path, notifier)

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        if fake.restarted:  # after the restart, exec stops working
            raise RuntimeError("403 Forbidden (proxy died mid-loop)")
        return ExecResult(4, "")  # genuine site failure -> breach

    fake.on_exec = handler

    mon.run_once()  # 1/2
    mon.run_once()  # 2/2 -> breach -> restart -> re-verify cannot run

    keys = notifier.event_keys()
    assert fake.restarted == ["gluetun"], "the breach was real; one restart is correct"
    assert "gluetun-recovered" not in keys, f"fake-green! {keys}"
    assert "gluetun-unprobeable" in keys, keys
    assert mon._unrecovered_sites, "triggering sites must be held, not presumed cleared"


def test_partial_exec_failure_does_not_gate_a_restart(tmp_path: Path) -> None:
    """One site unprobeable, one healthy: the unprobeable one is simply unevaluated —
    it never accrues a failure counter, so it can never breach."""
    notifier = FakeNotifier()
    mon, fake = _monitor(tmp_path, notifier)

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        if cmd and A in cmd:
            raise RuntimeError("403 Forbidden (this one probe can't run)")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler

    for _ in range(6):
        mon.run_once()

    assert fake.restarted == [], "an unprobeable site must never gate a restart"
    # Not ALL probes failed, so this isn't the whole-gateway alert.
    assert "gluetun-unprobeable" not in notifier.event_keys()


def test_unprobeable_resolves_once_probing_works_again(tmp_path: Path) -> None:
    """Lifecycle: the alert clears itself when exec starts working, exactly once."""
    notifier = FakeNotifier()
    mon, fake = _monitor(tmp_path, notifier)
    state = {"denied": True}

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if state["denied"]:
            raise RuntimeError("403 Forbidden")
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler

    mon.run_once()
    mon.run_once()
    assert "gluetun-unprobeable" in notifier.event_keys()

    state["denied"] = False  # EXEC permission restored
    mon.run_once()

    keys = notifier.event_keys()
    assert keys.count("resolve:gluetun-unprobeable") == 1, keys
    assert fake.restarted == [], "recovery of the probe path must not trigger a restart"
