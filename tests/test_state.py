"""Consecutive-failure counters (in-memory, reset-on-pass, no persistence)."""

from __future__ import annotations

from gluetun_monitor.state import Counter


def test_fail_increments_consecutively() -> None:
    c = Counter()
    assert c.fail("x") == 1
    assert c.fail("x") == 2
    assert c.fail("x") == 3
    assert c.get("x") == 3


def test_reset_zeroes_one_key() -> None:
    c = Counter()
    c.fail("x")
    c.fail("y")
    c.reset("x")
    assert c.get("x") == 0
    assert c.get("y") == 1


def test_get_unknown_key_is_zero() -> None:
    assert Counter().get("never-seen") == 0


def test_reset_all() -> None:
    c = Counter()
    c.fail("a")
    c.fail("b")
    c.reset_all()
    assert c.get("a") == 0
    assert c.get("b") == 0
