"""Malformed env values are collected as fatal errors, not silently swallowed."""

from __future__ import annotations

import pytest

from gluetun_monitor.config import Config


def test_invalid_int_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_INTERVAL", "abc")
    c = Config.from_env()
    assert any("CHECK_INTERVAL" in e for e in c.errors)


def test_invalid_bool_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_RECREATE", "maybe")
    c = Config.from_env()
    assert any("AUTO_RECREATE" in e for e in c.errors)


def test_invalid_log_level_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    c = Config.from_env()
    assert any("LOG_LEVEL" in e for e in c.errors)


def test_clean_env_has_no_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CHECK_INTERVAL", "TIMEOUT", "FAIL_THRESHOLD", "HEALTHY_WAIT_TIMEOUT",
                "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS", "AUTO_RECREATE",
                "DNS_WAIT_TIMEOUT", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    assert Config.from_env().errors == ()
