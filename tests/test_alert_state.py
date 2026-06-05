"""The edge-triggered active-alert lifecycle (ADR-0012).

Covers the four transitions an ongoing problem can make — new, reminder (per
NOTIFY_REPEAT_INTERVAL loops), resolved (cleared, with subject still live), and
silent-removed (forgotten) — plus persistence across a restart of the monitor.
"""

from __future__ import annotations

import io
from pathlib import Path

from gluetun_monitor.alert_state import AlertState
from gluetun_monitor.logging_setup import Logger


def _log() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _state(path: str | None = None, *, repeat: int = 0) -> AlertState:
    return AlertState(path, logger=_log(), repeat_interval=repeat)


def test_new_problem_emits_once_then_silent_at_repeat_zero() -> None:
    s = _state(repeat=0)
    s.begin_loop()
    s.report("k", "attention", "title", "body")
    events = s.events()
    assert len(events) == 1
    assert events[0].key == "k" and events[0].tier == "attention"

    s.begin_loop()
    s.report("k", "attention", "title", "body")  # still active
    assert s.events() == []  # repeat=0 → announce once, then silence


def test_repeat_interval_reminds_every_n_loops() -> None:
    s = _state(repeat=2)
    s.begin_loop()
    s.report("k", "attention", "t", "b")
    assert len(s.events()) == 1  # loop 1: new
    s.begin_loop()
    s.report("k", "attention", "t", "b")
    assert s.events() == []  # loop 2: 1 < 2 → suppressed
    s.begin_loop()
    s.report("k", "attention", "t", "b")
    reminders = s.events()  # loop 3: 2 >= 2 → reminder
    assert len(reminders) == 1
    assert reminders[0].title.startswith("still active")


def test_resolve_when_condition_clears() -> None:
    s = _state()
    s.begin_loop()
    s.report("k", "attention", "The problem", "b")
    s.events()
    s.begin_loop()  # not reported this loop → it cleared
    resolved = s.events()
    assert len(resolved) == 1
    assert resolved[0].key == "resolve:k"
    assert resolved[0].tier == "attention"  # closure reaches the same floor as the alert
    assert "resolved" in resolved[0].title.lower()


def test_forgotten_subject_clears_silently() -> None:
    s = _state()
    s.begin_loop()
    s.report("k", "attention", "P", "b")
    s.events()
    s.begin_loop()
    s.forget("k")  # subject removed (site dropped / dependent excluded)
    assert s.events() == []  # silent — no false "resolved"


def test_active_count_reflects_firing_problems() -> None:
    s = _state()
    s.begin_loop()
    s.report("a", "attention", "x", "y")
    s.report("b", "attention", "x", "y")
    s.events()
    assert s.active_count() == 2


def test_persistence_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "notify-state.json")

    s1 = AlertState(path, logger=_log(), repeat_interval=0)
    s1.begin_loop()
    s1.report("k", "attention", "P", "b")
    assert len(s1.events()) == 1  # announced
    s1.save()

    # Restart: still broken → must NOT re-announce (the re-spam this prevents).
    s2 = AlertState(path, logger=_log(), repeat_interval=0)
    s2.begin_loop()
    s2.report("k", "attention", "P", "b")
    assert s2.events() == []
    s2.save()

    # Restart: now cleared → the resolve fires even though it healed while down.
    s3 = AlertState(path, logger=_log(), repeat_interval=0)
    s3.begin_loop()
    resolved = s3.events()
    assert len(resolved) == 1 and resolved[0].key == "resolve:k"


def test_corrupt_sidecar_starts_clean(tmp_path: Path) -> None:
    path = tmp_path / "notify-state.json"
    path.write_text("{ not valid json")
    s = AlertState(str(path), logger=_log(), repeat_interval=0)
    s.begin_loop()
    assert s.active_count() == 0  # degraded to empty, no crash
