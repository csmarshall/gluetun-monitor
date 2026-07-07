"""#107: a regression harness for the notification state machine.

The #106 false-resolve flap shipped undetected because nothing asserted the
*emergent, loop-level* notification behavior — the interaction of the breach
counter, the post-restart ``reset_all()``, and the ``AlertState`` lifecycle over a
multi-loop failure → restart → recovery sequence. Unit tests covered each
transition in isolation; none pinned the emitted *event stream* across N loops
against a persistently-failing subject.

This module is that harness. Each scenario:

1. Scripts a per-loop world (which sites fail their probe, and whether a restart
   heals them) via the existing :class:`FakeDockerClient.on_exec` seam.
2. Steps ``run_once`` once per scripted loop.
3. Asserts the exact ordered stream of ``(subject, kind)`` the notifier received,
   where *kind* ∈ {new, reminder, resolve, retire} is decoded from the event key
   (``resolve:`` / ``deprecated:`` prefixes) and title (``still active:``). A
   supersede emits nothing (silent, by design — ADR-0012).

The notifier here is :class:`FakeNotifier`, which records **every** event the loop
flushes (tier filtering lives downstream in ``AppriseNotifier``), so the stream is
the complete, unfiltered lifecycle — which is exactly what we want to lock down.

Coverage note: this harness drives the **gluetun-site → restart → unrecovered/
recovered** path (the #106 class) plus the **subject-removed → retire** path below.
Row 5 of the issue table — the dependent unhealthy→wedged *supersede* sequence
(#98) — is already locked by ``tests/test_wedge_escalation.py`` (it asserts the
``dependent-unhealthy`` alert is superseded by ``dependent-wedged`` with no false
``resolve``), so it isn't duplicated here.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.notify import NotifyEvent
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64
SITE = "https://blocked.example"
GOOD = "https://1.1.1.1"


# ----- event-stream classification -----


def classify(event: NotifyEvent) -> tuple[str, str]:
    """Decode a raw :class:`NotifyEvent` into ``(subject, kind)``.

    The lifecycle encodes kind in the key/title (ADR-0012): a ``resolve:`` or
    ``deprecated:`` prefix, a ``still active:`` title for a repeat reminder, else a
    fresh announcement. ``subject`` is the key with any lifecycle prefix stripped, so
    ``resolve:gluetun-unrecovered`` and its original ``gluetun-unrecovered`` share a
    subject and read as one episode across kinds.
    """
    key = event.key
    if key.startswith("resolve:"):
        return key[len("resolve:"):], "resolve"
    if key.startswith("deprecated:"):
        return key[len("deprecated:"):], "retire"
    if event.title.startswith("still active: "):
        return key, "reminder"
    return key, "new"


# ----- scripted world -----


@dataclass
class Loop:
    """One scripted monitor loop.

    ``failing`` is the set of site URLs whose probe fails this loop. ``heal_on_restart``
    controls the *within-loop* re-verify after a gluetun restart: True models a genuine
    outage the restart fixes (post-restart probe passes); False models a site that stays
    down even after the tunnel bounces (→ ``gluetun-unrecovered``).
    """

    failing: set[str] = field(default_factory=set)
    heal_on_restart: bool = True


@dataclass
class _World:
    """Mutable per-loop state the exec handler reads (set by :func:`drive`)."""

    failing: set[str] = field(default_factory=set)
    heal_on_restart: bool = True
    restart_baseline: int = 0


def _build(tmp_path: Path, notifier: FakeNotifier, world: _World, fake: FakeDockerClient,
           **cfg_over: object) -> Monitor:
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{SITE}\n")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")  # DNS-stability probe always passes here
        url = cmd[-1] if cmd else ""
        if url in world.failing:
            restarted_this_loop = len(fake.restarted) > world.restart_baseline
            if world.heal_on_restart and restarted_this_loop:
                return ExecResult(0, "")  # the restart fixed it (within-loop re-verify)
            return ExecResult(4, "")  # site times out
        return ExecResult(0, "")

    fake.on_exec = handler
    cfg = Config(
        config_file=str(conf),
        gluetun_container="gluetun",
        fail_threshold=2,            # 2 consecutive fails to breach (matches #106 test)
        dns_wait_timeout=2,
        advisory_min_restarts=1000,  # keep the flaky-site nag out of these assertions
        **cfg_over,  # type: ignore[arg-type]
    )
    return Monitor(
        fake, cfg, Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )


def drive(tmp_path: Path, loops: list[Loop], notifier: FakeNotifier | None = None,
          **cfg_over: object) -> tuple[Monitor, FakeNotifier]:
    """Run ``loops`` through a fresh monitor and return ``(monitor, notifier)``."""
    notifier = notifier or FakeNotifier()
    world = _World()
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    mon = _build(tmp_path, notifier, world, fake, **cfg_over)
    for spec in loops:
        world.failing = spec.failing
        world.heal_on_restart = spec.heal_on_restart
        world.restart_baseline = len(fake.restarted)  # "did a restart happen THIS loop"
        mon.run_once()
    return mon, notifier


def stream(notifier: FakeNotifier) -> list[tuple[str, str]]:
    """The classified ``(subject, kind)`` event stream, in order."""
    return [classify(e) for e in notifier.events]


# ----- scenarios (issue #107 seed table) -----


def test_site_down_forever_announces_unrecovered_once(tmp_path: Path) -> None:
    """Row 1: one site fails forever, tunnel otherwise healthy → ``unrecovered`` is
    announced exactly once and never (falsely) resolves, however many restart cycles
    the persistent failure churns through."""
    loops = [Loop(failing={SITE}, heal_on_restart=False) for _ in range(6)]
    _mon, notifier = drive(tmp_path, loops)
    s = stream(notifier)

    unrecovered = [ev for ev in s if ev[0] == "gluetun-unrecovered"]
    assert unrecovered == [("gluetun-unrecovered", "new")], s
    assert ("gluetun-unrecovered", "resolve") not in s, s


def test_genuine_outage_restart_heals_no_unrecovered(tmp_path: Path) -> None:
    """Row 2: a genuine outage the restart fixes → a ``gluetun-restart`` point event
    and one ``gluetun-recovered``; the ``unrecovered`` alert never fires."""
    loops = [Loop(failing={SITE}, heal_on_restart=True) for _ in range(2)]
    _mon, notifier = drive(tmp_path, loops)
    s = stream(notifier)

    assert ("gluetun-restart", "new") in s, s
    assert ("gluetun-recovered", "new") in s, s
    assert not any(subject == "gluetun-unrecovered" for subject, _ in s), s


def test_outage_restart_fails_then_clears_resolves_once(tmp_path: Path) -> None:
    """Row 3: outage → restart doesn't fix it (``unrecovered`` new) → the site later
    clears on its own → exactly one ``resolve`` and no re-announce."""
    # Four loops down (restart never heals) → unrecovered; then two clean loops.
    loops = [Loop(failing={SITE}, heal_on_restart=False) for _ in range(4)]
    loops += [Loop(failing=set()) for _ in range(2)]
    _mon, notifier = drive(tmp_path, loops)
    s = stream(notifier)

    episode = [ev for ev in s if ev[0] == "gluetun-unrecovered"]
    assert episode == [("gluetun-unrecovered", "new"), ("gluetun-unrecovered", "resolve")], s


def test_flapping_site_below_threshold_is_silent(tmp_path: Path) -> None:
    """Row 4: a site that flaps around the threshold (never two fails in a row) never
    sustains a breach → no restart, no alert, no notification storm."""
    loops = []
    for i in range(8):
        loops.append(Loop(failing={SITE} if i % 2 == 0 else set()))
    mon, notifier = drive(tmp_path, loops)

    assert stream(notifier) == [], stream(notifier)
    assert mon.client.restarted == []  # never breached → never restarted


def test_repeat_interval_re_announces_as_reminder(tmp_path: Path) -> None:
    """The lifecycle's NOTIFY_REPEAT_INTERVAL rung: a persistent ``unrecovered`` alert
    re-announces as a *reminder* (not a fresh ``new``) every ``repeat_interval`` loops,
    so an operator isn't left wondering whether a long outage is still ongoing."""
    loops = [Loop(failing={SITE}, heal_on_restart=False) for _ in range(8)]
    # Persistence is required for the lifecycle to run; enable a notifier URL + sidecar.
    _mon, notifier = drive(
        tmp_path, loops,
        apprise_urls=("json://localhost",),
        notify_state_file=str(tmp_path / "notify-state.json"),
        notify_repeat_interval=3,
    )
    s = stream(notifier)
    kinds = [kind for subject, kind in s if subject == "gluetun-unrecovered"]
    assert kinds[0] == "new", s
    assert "reminder" in kinds, s
    assert kinds.count("new") == 1, s  # announced once; the rest are reminders


def test_removed_advisory_site_is_retired_not_resolved(tmp_path: Path) -> None:
    """Row 6: a ``role=advisory`` site that's unreachable raises an ``advisory-down``
    alert; when it's dropped from sites.conf the alert must **retire** ("no longer
    monitored") — not falsely **resolve**. Its subject is gone, so we cannot claim it
    recovered; a resolve there would be a lie (ADR-0012).

    Uses a bespoke setup (not :func:`drive`) because it needs an advisory-role site
    and a mid-run config edit — the two levers the generic gluetun-path driver
    deliberately doesn't expose.
    """
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{SITE}|role=advisory\n")
    notifier = FakeNotifier()
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        url = cmd[-1] if cmd else ""
        return ExecResult(4, "") if url == SITE else ExecResult(0, "")

    fake.on_exec = handler
    cfg = Config(
        config_file=str(conf), gluetun_container="gluetun",
        fail_threshold=2, dns_wait_timeout=2, advisory_min_restarts=1000,
    )
    mon = Monitor(
        fake, cfg, Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )
    mon.run_once()  # advisory 1/2
    mon.run_once()  # advisory 2/2 -> advisory-down alert announced
    assert (f"advisory-down:{SITE}", "new") in stream(notifier)

    # Drop the advisory site from config; next loop's change-detection forgets it.
    conf.write_text(f"{GOOD}\n")
    mon.run_once()

    s = stream(notifier)
    assert (f"advisory-down:{SITE}", "retire") in s, s
    assert (f"advisory-down:{SITE}", "resolve") not in s, s
