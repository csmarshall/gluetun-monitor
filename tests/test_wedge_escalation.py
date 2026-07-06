"""Wedge escalation + retry backoff (#98).

Why: observed live, twice. (1) The #76 dogfood left an unremovable parked twin
(ZFS "dataset is busy"); the guard correctly refused remediation — and then the
monitor drove the same doomed removal through the daemon every 30s for ~14
hours, surfacing only a generic dependent-unhealthy alert. (2) A dependent
stuck in "marked for removal" got the same identical-failure restart loop for
hours, across a host reboot. These tests pin the fix: N consecutive IDENTICAL
failures escalate to a distinct ``dependent-wedged`` alert (runbook included
when the blocker is the parked twin) and remediation attempts back off with a
doubling, capped interval — while probing continues every loop, so a cleared
wedge heals promptly and a changed failure starts the count over.
"""

from __future__ import annotations

import io
import random
from pathlib import Path
from typing import Any

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.monitor_state import MonitorState
from gluetun_monitor.recovery import remediate_dependent

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "c" * 64
PARKED = "dep.gm-recreate-old"
WEDGED_KEY = "dependent-wedged:dep"
UNHEALTHY_KEY = "dependent-unhealthy:dep"


class _WedgedRemove(FakeDockerClient):
    """Remove of the parked twin raises (the live ZFS wedge) while ``blocked``."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked = True
        self.blocked_attempts = 0

    def remove(self, name_or_id: str, *, volumes: bool) -> None:
        if self.blocked and name_or_id.endswith(".gm-recreate-old"):
            self.blocked_attempts += 1
            raise RuntimeError(
                'driver "zfs" failed to remove root filesystem: dataset is busy'
            )
        super().remove(name_or_id, volumes=volumes)


def _monitor(
    fake: FakeDockerClient, tmp_path: Path, **cfg: Any
) -> tuple[Monitor, FakeNotifier, io.StringIO]:
    (tmp_path / "sites.conf").write_text("https://a.example\n")
    cfg.setdefault("config_file", str(tmp_path / "sites.conf"))
    cfg.setdefault("gluetun_container", "gluetun")
    cfg.setdefault("monitor_state_file", str(tmp_path / "state.json"))
    cfg.setdefault("stats_file", str(tmp_path / "stats.json"))
    cfg.setdefault("notify_state_file", str(tmp_path / "notify.json"))
    # Discovery matches RUNNING containers only; a wedged dependent is typically
    # stopped (replacement created but never started). In production it stays
    # visible via the durable dependent memory (#97) — seed it the same way.
    seed = MonitorState(str(tmp_path / "state.json"))
    seed.record_gluetun_id(GLUETUN_ID)
    seed.set_known_dependents({"dep"})
    seed.save()
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    notifier = FakeNotifier()
    mon = Monitor(
        fake, Config(**cfg), logger, rng=random.Random(0), sleep=lambda _s: None,
        notifier=notifier,
    )

    def running_aware(name: str, cmd: list[str]) -> ExecResult:
        info = fake.inspect(name)
        if info is None or not info.running:
            return ExecResult(126, "container is not running")
        return ExecResult(0, "eth0\nlo\ntun0\n")

    fake.on_exec = running_aware
    return mon, notifier, stream


def _wedged_stack(fake: FakeDockerClient) -> None:
    """gluetun + a stopped dependent with an (unremovable) parked twin alongside
    — the exact post-'remove-old failed' state from the live incident."""
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}", running=False)
    parked = fake.add_container(
        PARKED, network_mode=f"container:{GLUETUN_ID}", running=False, health=None
    )
    parked.raw["State"]["Status"] = "dead"  # the zombie tell from the field


# ----- remediate_dependent: the wedge is reported, not just logged -----


def test_outcome_carries_the_wedge(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    out = remediate_dependent(
        fake, "dep", GLUETUN_ID, Config(),
        Logger(log_file=None, level="DEBUG", stream=io.StringIO()),
        sleep=lambda _s: None,
    )
    assert bool(out) is False
    assert out.wedge is not None
    assert out.wedge.parked_name == PARKED
    assert out.wedge.parked_status == "dead"
    assert "dataset is busy" in out.wedge.error
    assert "dataset is busy" in out.detail  # the signature the monitor compares


# ----- escalation: N identical failures → distinct alert, generic superseded -----


def test_escalates_after_n_identical_failures(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    mon, notifier, stream = _monitor(fake, tmp_path, wedge_escalate_after=3)

    mon.run_once()
    mon.run_once()
    assert UNHEALTHY_KEY in notifier.event_keys()  # pre-threshold: generic alert
    assert WEDGED_KEY not in notifier.event_keys()

    mon.run_once()  # third identical failure → escalate
    assert WEDGED_KEY in notifier.event_keys()
    assert "is WEDGED" in stream.getvalue()
    # The generic alert was superseded, NOT resolved (the problem didn't clear).
    assert f"resolve:{UNHEALTHY_KEY}" not in notifier.event_keys()


def test_wedged_alert_carries_error_state_and_runbook(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    mon, notifier, _ = _monitor(fake, tmp_path, wedge_escalate_after=3)
    for _ in range(3):
        mon.run_once()

    wedged = [e for e in notifier.events if e.key == WEDGED_KEY]
    assert wedged, "escalation alert was never emitted"
    body = wedged[0].body
    assert "dataset is busy" in body  # the exact driver error
    assert PARKED in body and "state: dead" in body  # inspect state of the twin
    # The runbook: locate via mountinfo, kill only cgroup-confirmed pids, rm -f.
    assert "/proc/*/mountinfo" in body
    assert "cgroup" in body
    assert f"docker rm -f {PARKED}" in body


# ----- backoff: doomed attempts are throttled, alert stays active -----


def test_backoff_doubles_and_caps_attempts(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    # interval 30s, cap 120s → backoff caps at 4 loops.
    mon, notifier, _ = _monitor(
        fake, tmp_path, wedge_escalate_after=3, check_interval=30, wedge_backoff_cap=120
    )
    attempts_by_loop = []
    for _ in range(16):
        mon.run_once()
        attempts_by_loop.append(fake.blocked_attempts)

    # Loops 1-3 attempt every loop (pre-threshold); escalation at loop 3 backs
    # off 2 loops (skip 4,5 → attempt 6), then 4 loops (skip 7-10 → attempt 11),
    # then caps at 4 (skip 12-15 → attempt 16).
    expected = [1, 2, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 6]
    assert attempts_by_loop == expected
    # The wedged alert is reported every loop while backing off — exactly one
    # announce (edge-triggered), and no resolve while it holds.
    assert notifier.event_keys().count(WEDGED_KEY) == 1
    assert f"resolve:{WEDGED_KEY}" not in notifier.event_keys()


# ----- the full lifecycle from the issue: wedge → operator clears → heal -----


def test_full_wedge_lifecycle_heals_via_restart_not_second_recreate(tmp_path: Path) -> None:
    """remove-old fails → GC blocks for N loops (replacement exists, created but
    not started, homed on the CURRENT gluetun id) → the twin becomes removable →
    the next attempted loop GCs it and heals the dependent via RESTART — no
    second recreate (the observed 14-hour arc, compressed)."""
    fake = _WedgedRemove()
    _wedged_stack(fake)
    mon, notifier, _ = _monitor(fake, tmp_path, wedge_escalate_after=3)

    for _ in range(4):  # 3 failures (escalate at 3rd), loop 4 skipped (backoff)
        mon.run_once()
    assert fake.inspect(PARKED) is not None
    assert not fake.inspect("dep").running  # type: ignore[union-attr]

    fake.blocked = False  # operator clears the ZFS pin (per the alert's runbook)
    mon.run_once()  # loop 5: still in backoff — skipped
    mon.run_once()  # loop 6: attempt → GC removes the twin, RESTART heals

    assert fake.inspect(PARKED) is None  # twin finally GC'd
    assert fake.inspect("dep").running  # type: ignore[union-attr]
    assert fake.created == []  # healed via restart, never a second recreate
    assert "dep" in fake.restarted
    # The wedged alert resolves and the recovery event fires.
    assert f"resolve:{WEDGED_KEY}" in notifier.event_keys()
    assert "dependent-remediated:dep" in notifier.event_keys()


def test_wedge_clears_when_dependent_recovers_without_us(tmp_path: Path) -> None:
    """Operator fixes everything by hand mid-backoff (twin gone, dependent up):
    the next loop sees it healthy, drops the bookkeeping, resolves the alert."""
    fake = _WedgedRemove()
    _wedged_stack(fake)
    mon, notifier, _ = _monitor(fake, tmp_path, wedge_escalate_after=3)
    for _ in range(3):
        mon.run_once()

    fake.blocked = False
    fake.remove(PARKED, volumes=False)
    fake.start("dep")
    mon.run_once()

    assert f"resolve:{WEDGED_KEY}" in notifier.event_keys()
    assert not mon._wedges  # bookkeeping dropped


# ----- a DIFFERENT failure is a new situation, not a deeper wedge -----


def test_changed_failure_signature_restarts_the_count(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    mon, notifier, _ = _monitor(fake, tmp_path, wedge_escalate_after=3)
    mon.run_once()
    mon.run_once()  # two identical failures — one short of escalation

    # The wedge morphs: twin now removable, but the restart starts failing
    # differently (e.g. "marked for removal").
    fake.blocked = False

    def failing_restart(_name: str) -> None:
        raise RuntimeError("container is marked for removal and cannot be started")

    fake.restart = failing_restart  # type: ignore[method-assign]
    mon.run_once()  # 3rd failure overall — but a NEW signature: no escalation yet
    assert WEDGED_KEY not in notifier.event_keys()
    mon.run_once()
    mon.run_once()  # 3rd IDENTICAL new-signature failure → now escalate
    wedged = [e for e in notifier.events if e.key == WEDGED_KEY]
    assert wedged and "marked for removal" in wedged[0].body


# ----- generic identical failures (no parked twin) escalate too -----


def test_restart_500_loop_escalates_without_a_parked_twin(tmp_path: Path) -> None:
    """The prowlarr field case: restart fails identically every loop ('marked
    for removal') with no parked twin involved — still escalates + backs off."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}", running=False)

    def failing_restart(_name: str) -> None:
        raise RuntimeError(
            "500 Server Error: container is marked for removal and cannot be started"
        )

    fake.restart = failing_restart  # type: ignore[method-assign]
    mon, notifier, _ = _monitor(fake, tmp_path, wedge_escalate_after=3)
    for _ in range(4):
        mon.run_once()

    wedged = [e for e in notifier.events if e.key == WEDGED_KEY]
    assert wedged and "marked for removal" in wedged[0].body
    assert "Runbook" not in wedged[0].body  # twin runbook only when there IS a twin


# ----- a monitor restart mid-wedge neither re-announces nor falsely resolves -----


def test_monitor_restart_mid_wedge_stays_escalated(tmp_path: Path) -> None:
    fake = _WedgedRemove()
    _wedged_stack(fake)
    # apprise_urls set → the alert lifecycle persists to the notify sidecar.
    cfg: dict[str, Any] = {"apprise_urls": ("json://localhost",), "wedge_escalate_after": 3}
    mon_a, notifier_a, _ = _monitor(fake, tmp_path, **cfg)
    for _ in range(3):
        mon_a.run_once()
    assert WEDGED_KEY in notifier_a.event_keys()

    # Fresh monitor instance, same sidecars (in-memory wedge bookkeeping lost).
    mon_b, notifier_b, _ = _monitor(fake, tmp_path, **cfg)
    mon_b.run_once()

    # Still escalated: no re-announce, no false resolve, no demotion to generic.
    assert WEDGED_KEY not in notifier_b.event_keys()
    assert f"resolve:{WEDGED_KEY}" not in notifier_b.event_keys()
    assert UNHEALTHY_KEY not in notifier_b.event_keys()
