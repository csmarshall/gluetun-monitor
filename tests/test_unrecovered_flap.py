"""#106: the gluetun-unrecovered alert must not false-resolve every restart cycle.

A site that is unreachable through an otherwise-healthy tunnel (e.g. a geo-blocked
endpoint) fails every loop. With FAIL_THRESHOLD>1 the sequence is:

    breach (N/N) -> restart -> re-verify still fails -> report "unrecovered" and
    reset_all() -> next loop the site is back at 1/N (sub-threshold) -> "not breached"

Pre-fix, that mechanical sub-threshold loop let AlertState auto-resolve the active
alert — a false "recovered" — then the next breach re-announced it, storming the
operator with new -> resolved -> new every couple of loops. The alert must instead
stay active until the sites that triggered it OBSERVABLY clear (mirrors the #75
advisory hardening).
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
BLOCKED = "https://blocked.example"
GOOD = "https://1.1.1.1"


def _monitor(tmp_path: Path, notifier: FakeNotifier, state: dict, **cfg_over):
    """A monitor over one persistently-blocked site + one healthy site.

    ``state["block"]`` toggles whether the blocked site fails this loop.
    """
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{BLOCKED}\n")

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")  # DNS-stability probe passes
        if BLOCKED in cmd and state["block"]:
            return ExecResult(4, "")  # blocked site times out
        return ExecResult(0, "")

    fake.on_exec = handler

    cfg = Config(
        config_file=str(conf),
        gluetun_container="gluetun",
        fail_threshold=2,             # so the post-restart reset yields a sub-threshold loop
        dns_wait_timeout=2,
        advisory_min_restarts=1000,   # keep the advisory out of these assertions
        **cfg_over,
    )
    return Monitor(
        fake, cfg, Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )


def test_unrecovered_does_not_false_resolve_while_site_still_down(tmp_path: Path) -> None:
    """The core #106 regression (RED pre-fix): one announce, never a false resolve."""
    notifier = FakeNotifier()
    state = {"block": True}
    mon = _monitor(tmp_path, notifier, state)

    # Six loops of the blocked site failing: breach/restart cycles interleaved with
    # the sub-threshold "not breached" loops that used to trigger the false resolve.
    for _ in range(6):
        mon.run_once()

    keys = notifier.event_keys()
    # Announced exactly once, and never (falsely) resolved while the site is down.
    assert keys.count("gluetun-unrecovered") == 1, keys
    assert "resolve:gluetun-unrecovered" not in keys, keys


def test_unrecovered_resolves_once_on_genuine_recovery(tmp_path: Path) -> None:
    """The fix must not swallow a TRUE resolve: once the site clears, resolve once."""
    notifier = FakeNotifier()
    state = {"block": True}
    mon = _monitor(tmp_path, notifier, state)

    for _ in range(4):
        mon.run_once()
    assert notifier.event_keys().count("gluetun-unrecovered") == 1

    # The blocked site starts responding again — genuine recovery.
    state["block"] = False
    mon.run_once()  # not breached, blocked site now passes -> observed recovery
    mon.run_once()  # stays resolved, no re-flap

    keys = notifier.event_keys()
    assert keys.count("resolve:gluetun-unrecovered") == 1, keys
    assert keys.count("gluetun-unrecovered") == 1, keys  # one episode, one announce


def test_unrecovered_hold_survives_monitor_restart(tmp_path: Path) -> None:
    """#106 restart-safety: a monitor restart loses the in-RAM triggering set, but
    the alert survives in the persisted sidecar. The next sub-threshold loop must
    still HOLD it (adopt the currently-failing sites), not false-resolve it."""
    state = {"block": True}
    notify_file = str(tmp_path / "notify-state.json")

    # Monitor A establishes the active unrecovered alert and persists it.
    mon_a = _monitor(
        tmp_path, FakeNotifier(), state,
        apprise_urls=("json://localhost",),  # enables AlertState persistence
        notify_state_file=notify_file,
    )
    mon_a.run_once()  # blocked at 1/2
    mon_a.run_once()  # blocked at 2/2 -> restart -> re-verify fails -> unrecovered
    assert mon_a.alerts.is_active("gluetun-unrecovered")

    # Monitor B is a fresh process sharing the sidecar: it reloads the active alert
    # but starts with an empty _unrecovered_sites (the RAM set didn't persist).
    notifier_b = FakeNotifier()
    mon_b = _monitor(
        tmp_path, notifier_b, state,
        apprise_urls=("json://localhost",),
        notify_state_file=notify_file,
    )
    assert mon_b.alerts.is_active("gluetun-unrecovered")
    assert mon_b._unrecovered_sites == set()  # memory lost across the "restart"

    mon_b.run_once()  # blocked at 1/2 -> not breached -> must HOLD, not resolve

    assert "resolve:gluetun-unrecovered" not in notifier_b.event_keys()
    assert mon_b.alerts.is_active("gluetun-unrecovered")
