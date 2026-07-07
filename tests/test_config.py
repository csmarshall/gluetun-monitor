"""Config defaults + env parsing.

Why: the env-var names and their defaults are a compatibility contract with v1.x
(documented in the README and pinned by the differential suite). These tests
guard against a default silently drifting and against env overrides not taking
effect — either of which would change behavior for existing deployments.
"""

from __future__ import annotations

import pytest

from gluetun_monitor.config import Config


def test_defaults_match_v1_contract() -> None:
    """The v1.x env defaults must not drift — existing users rely on them."""
    c = Config()
    assert c.config_file == "/config/sites.conf"
    assert c.log_file == "/logs/gluetun-monitor.log"
    assert c.check_interval == 30
    assert c.timeout == 10
    assert c.fail_threshold == 2
    assert c.gluetun_container == "gluetun"
    assert c.healthy_wait_timeout == 120
    assert c.dependent_containers == "auto"
    assert c.docker_host is None


def test_v2_defaults() -> None:
    """The new v2 knobs default to safe, on-by-default behavior (Tenet 7)."""
    c = Config()
    assert c.dependent_container_failures == 2  # mirrors fail_threshold
    assert c.max_parallel_checks == 6
    assert c.auto_recreate is True
    assert c.log_level == "INFO"


def test_from_env_defaults_with_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env set, from_env() must equal the dataclass defaults — i.e. the
    two default sources can't diverge."""
    for var in (
        "CONFIG_FILE", "LOG_FILE", "CHECK_INTERVAL", "TIMEOUT", "FAIL_THRESHOLD",
        "GLUETUN_CONTAINER", "HEALTHY_WAIT_TIMEOUT", "DEPENDENT_CONTAINERS",
        "DOCKER_HOST", "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS",
        "AUTO_RECREATE", "LOG_LEVEL", "DNS_WAIT_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)
    assert Config.from_env() == Config()


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env values actually override the defaults (the basic config path works)."""
    monkeypatch.setenv("CHECK_INTERVAL", "15")
    monkeypatch.setenv("FAIL_THRESHOLD", "5")
    monkeypatch.setenv("GLUETUN_CONTAINER", "vpn")
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    c = Config.from_env()
    assert c.check_interval == 15
    assert c.fail_threshold == 5
    assert c.gluetun_container == "vpn"
    assert c.docker_host == "tcp://proxy:2375"


def test_dependent_container_failures_defaults_to_fail_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset, the dependent threshold mirrors FAIL_THRESHOLD — one mental model
    for the whole stack (ADR-0006)."""
    monkeypatch.setenv("FAIL_THRESHOLD", "4")
    monkeypatch.delenv("DEPENDENT_CONTAINER_FAILURES", raising=False)
    assert Config.from_env().dependent_container_failures == 4


def test_dependent_container_failures_independent_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """...but it can still be tuned independently of FAIL_THRESHOLD when set."""
    monkeypatch.setenv("FAIL_THRESHOLD", "4")
    monkeypatch.setenv("DEPENDENT_CONTAINER_FAILURES", "2")
    assert Config.from_env().dependent_container_failures == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("true", True), ("false", False),
     ("yes", True), ("on", True), ("off", False), ("no", False)],
)
def test_auto_recreate_bool_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """All documented boolean spellings parse correctly (so AUTO_RECREATE=yes
    isn't silently treated as off)."""
    monkeypatch.setenv("AUTO_RECREATE", value)
    assert Config.from_env().auto_recreate is expected


def test_int_fallback_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unparseable int still yields the default value on the object (the CLI
    separately treats the recorded error as fatal — see test_config_warnings)."""
    monkeypatch.setenv("CHECK_INTERVAL", "not-a-number")
    assert Config.from_env().check_interval == 30


def test_absurd_global_timeout_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fat-fingered global TIMEOUT is fatal-at-startup like the per-URL cap (#77),
    not silently accepted to stall every loop for hours (review finding)."""
    monkeypatch.setenv("TIMEOUT", "100000")
    assert any("TIMEOUT" in e and "300" in e for e in Config.from_env().errors)


def test_absurd_global_tries_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WGET_TRIES", "100000")
    assert any("WGET_TRIES" in e and "5" in e for e in Config.from_env().errors)
