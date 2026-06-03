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


def test_invalid_float_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ADVISORY_DOMINANCE is a fatal config error (not silently default)."""
    monkeypatch.setenv("ADVISORY_DOMINANCE", "half")
    c = Config.from_env()
    assert any("ADVISORY_DOMINANCE" in e for e in c.errors)


def test_invalid_log_level_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown LOG_LEVEL errors rather than silently defaulting to INFO."""
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    c = Config.from_env()
    assert any("LOG_LEVEL" in e for e in c.errors)


@pytest.mark.parametrize(
    "var",
    ["CHECK_INTERVAL", "TIMEOUT", "WGET_TRIES", "FAIL_THRESHOLD",
     "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS", "HEALTHY_WAIT_TIMEOUT",
     "ADVISORY_WINDOW", "ADVISORY_MIN_RESTARTS"],
)
def test_zero_is_rejected_for_must_be_positive_dials(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """0 (and negatives) on these would cause real bugs — infinite wget timeout,
    busy-loops, restart-on-every-loop — so they're fatal, not silently accepted."""
    monkeypatch.setenv(var, "0")
    assert any(var in e for e in Config.from_env().errors)


@pytest.mark.parametrize("var", ["TIMEOUT", "WGET_TRIES", "MAX_PARALLEL_CHECKS"])
def test_negative_is_rejected(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "-3")
    assert any(var in e for e in Config.from_env().errors)


def test_advisory_dominance_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dominance is a fraction; >1 (never fires) or <0 (always) are fatal."""
    monkeypatch.setenv("ADVISORY_DOMINANCE", "9")
    assert any("ADVISORY_DOMINANCE" in e for e in Config.from_env().errors)
    monkeypatch.setenv("ADVISORY_DOMINANCE", "-0.5")
    assert any("ADVISORY_DOMINANCE" in e for e in Config.from_env().errors)


def test_in_range_values_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEOUT", "30")
    monkeypatch.setenv("WGET_TRIES", "3")
    monkeypatch.setenv("ADVISORY_DOMINANCE", "0.75")
    c = Config.from_env()
    assert c.errors == ()
    assert c.timeout == 30 and c.wget_tries == 3 and c.advisory_dominance == 0.75


def test_intentional_special_values_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented sentinels are NOT rejected: STATS_RETENTION_DAYS<=0 = keep
    forever, LOG_MAX_BYTES=0 = no rotation, DEPENDENT_VIABILITY_SAMPLES=-1 = all."""
    monkeypatch.setenv("STATS_RETENTION_DAYS", "0")
    monkeypatch.setenv("LOG_MAX_BYTES", "0")
    monkeypatch.setenv("DEPENDENT_VIABILITY_SAMPLES", "-1")
    c = Config.from_env()
    assert c.errors == ()
    assert c.stats_retention_days == 0
    assert c.log_max_bytes == 0
    assert c.dependent_viability_samples == -1


def test_viability_samples_below_minus_one_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPENDENT_VIABILITY_SAMPLES", "-5")
    assert any("DEPENDENT_VIABILITY_SAMPLES" in e for e in Config.from_env().errors)


def test_viability_samples_zero_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 would probe nothing and the monitor would silently coerce it to 1, so
    it's fatal — only -1 (all) or a positive count are valid."""
    monkeypatch.setenv("DEPENDENT_VIABILITY_SAMPLES", "0")
    assert any("DEPENDENT_VIABILITY_SAMPLES" in e for e in Config.from_env().errors)


def test_viability_samples_one_and_minus_one_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "-1", "5"):
        monkeypatch.setenv("DEPENDENT_VIABILITY_SAMPLES", val)
        assert Config.from_env().errors == ()


def test_clean_env_has_no_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crucial contrast: with everything unset, there are NO errors — unset
    is just the default, never a misconfiguration."""
    for var in ("CHECK_INTERVAL", "TIMEOUT", "FAIL_THRESHOLD", "HEALTHY_WAIT_TIMEOUT",
                "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS", "AUTO_RECREATE",
                "DNS_WAIT_TIMEOUT", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    assert Config.from_env().errors == ()
