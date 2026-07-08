"""#45: the dependent-flapping advisory.

A dependent that keeps needing remediation — N times within a window — escalates
from the per-loop `recovery` events to an `attention` alert ("it won't stay
healthy; investigate"), announced once and resolved by the lifecycle when it
settles. Count-based (no dominance): the dependent analogue of the flaky-site
advisory (#75), mirrored but simpler.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _monitor(notifier: FakeNotifier, clock: _Clock) -> tuple[Monitor, SiteStatsStore]:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    stats = SiteStatsStore(None, clock=clock)
    mon = Monitor(
        fake,
        Config(gluetun_container="gluetun",
               dependent_advisory_min_remediations=3,
               dependent_advisory_window=100),
        Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=stats, notifier=notifier,
    )
    return mon, stats


def _cycle(mon: Monitor) -> None:
    """One lifecycle cycle — exactly what run_once does around the advisory."""
    mon.alerts.begin_loop()
    mon._emit_dependent_advisories()
    mon._flush_notifications()


def test_flapping_dependent_escalates_to_attention_once() -> None:
    """At >= threshold remediations in the window: one announce, no re-announce while
    it keeps flapping, no false resolve."""
    notifier = FakeNotifier()
    stats_clock = _Clock(1000.0)
    mon, stats = _monitor(notifier, stats_clock)
    for _ in range(3):
        stats.record_dependent_remediation("qbittorrent")

    _cycle(mon)
    _cycle(mon)  # still flapping -> stays active, no fresh announce

    keys = notifier.event_keys()
    assert keys.count("dependent-flapping:qbittorrent") == 1, keys
    assert "resolve:dependent-flapping:qbittorrent" not in keys, keys


def test_flapping_advisory_resolves_when_it_settles() -> None:
    """Once the remediations age out of the window, the alert resolves exactly once."""
    notifier = FakeNotifier()
    stats_clock = _Clock(1000.0)
    mon, stats = _monitor(notifier, stats_clock)
    for _ in range(3):
        stats.record_dependent_remediation("qbittorrent")

    _cycle(mon)
    assert "dependent-flapping:qbittorrent" in notifier.event_keys()

    stats_clock.t += 200  # all three remediations now outside the 100s window
    _cycle(mon)

    keys = notifier.event_keys()
    assert keys.count("resolve:dependent-flapping:qbittorrent") == 1, keys


def test_below_threshold_dependent_is_silent() -> None:
    notifier = FakeNotifier()
    stats_clock = _Clock(1000.0)
    mon, stats = _monitor(notifier, stats_clock)
    for _ in range(2):  # below the min of 3
        stats.record_dependent_remediation("prowlarr")

    _cycle(mon)
    assert notifier.events == []


def test_two_dependents_flap_independently() -> None:
    """No dominance — each dependent is judged on its own count, both can fire."""
    notifier = FakeNotifier()
    stats_clock = _Clock(1000.0)
    mon, stats = _monitor(notifier, stats_clock)
    for _ in range(3):
        stats.record_dependent_remediation("qbittorrent")
    for _ in range(4):
        stats.record_dependent_remediation("prowlarr")

    _cycle(mon)

    keys = notifier.event_keys()
    assert "dependent-flapping:qbittorrent" in keys
    assert "dependent-flapping:prowlarr" in keys
