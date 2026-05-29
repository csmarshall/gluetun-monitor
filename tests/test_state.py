"""Consecutive-failure counters (in-memory, reset-on-pass, no persistence).

Why: these counters are the whole of the monitor's per-site / per-dependent
memory (ADR-0006 / Tenet 8 — re-act rather than remember). The behavior that
matters is that failures count *consecutively* and a reset truly zeroes, since
that's what turns "N failures in a row" into a remediation trigger.
"""

from __future__ import annotations

from gluetun_monitor.state import Counter


def test_fail_increments_consecutively() -> None:
    """fail() returns a monotonically increasing count for the same key — this is
    what the threshold checks compare against."""
    c = Counter()
    assert c.fail("x") == 1
    assert c.fail("x") == 2
    assert c.fail("x") == 3
    assert c.get("x") == 3


def test_reset_zeroes_one_key() -> None:
    """reset() clears only its own key — a passing site/dependent must not reset
    another's accumulated failures."""
    c = Counter()
    c.fail("x")
    c.fail("y")
    c.reset("x")
    assert c.get("x") == 0
    assert c.get("y") == 1


def test_get_unknown_key_is_zero() -> None:
    """An unseen key reads as 0, so first-failure logic doesn't need to pre-seed."""
    assert Counter().get("never-seen") == 0


def test_reset_all() -> None:
    """reset_all() zeroes every key — used after a successful gluetun recovery to
    give the whole stack a clean slate."""
    c = Counter()
    c.fail("a")
    c.fail("b")
    c.reset_all()
    assert c.get("a") == 0
    assert c.get("b") == 0
