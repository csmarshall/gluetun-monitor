"""Malformed env values are collected as fatal errors, not silently swallowed.

Why: "bad config is fatal, don't guess" (Tenet 1) — a watchdog acting on the
system must not run with parameters it couldn't parse. from_env records each
malformed value in .errors; the CLI refuses to start if any are present. These
tests pin that *unset* (sane default) and *set-but-bad* (error) are distinct.
"""

from __future__ import annotations

import pytest

from gluetun_monitor.config import Config


def test_invalid_int_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer CHECK_INTERVAL is recorded as a fatal error (not silently
    defaulted, as v1 did)."""
    monkeypatch.setenv("CHECK_INTERVAL", "abc")
    c = Config.from_env()
    assert any("CHECK_INTERVAL" in e for e in c.errors)


def test_invalid_bool_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized AUTO_RECREATE value errors rather than being coerced to
    False — a silent coercion could disable recreate without the user knowing."""
    monkeypatch.setenv("AUTO_RECREATE", "maybe")
    c = Config.from_env()
    assert any("AUTO_RECREATE" in e for e in c.errors)


def test_invalid_log_level_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown LOG_LEVEL errors rather than silently defaulting to INFO."""
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    c = Config.from_env()
    assert any("LOG_LEVEL" in e for e in c.errors)


def test_clean_env_has_no_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crucial contrast: with everything unset, there are NO errors — unset
    is just the default, never a misconfiguration."""
    for var in ("CHECK_INTERVAL", "TIMEOUT", "FAIL_THRESHOLD", "HEALTHY_WAIT_TIMEOUT",
                "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS", "AUTO_RECREATE",
                "DNS_WAIT_TIMEOUT", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    assert Config.from_env().errors == ()
