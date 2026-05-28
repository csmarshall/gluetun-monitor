"""Config defaults + env parsing. The v1.x defaults are part of the contract."""

from __future__ import annotations

import pytest

from gluetun_monitor.config import Config


def test_defaults_match_v1_contract() -> None:
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
    c = Config()
    assert c.dependent_container_failures == 2  # mirrors fail_threshold
    assert c.max_parallel_checks == 6
    assert c.auto_recreate is True
    assert c.log_level == "INFO"


def test_from_env_defaults_with_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CONFIG_FILE", "LOG_FILE", "CHECK_INTERVAL", "TIMEOUT", "FAIL_THRESHOLD",
        "GLUETUN_CONTAINER", "HEALTHY_WAIT_TIMEOUT", "DEPENDENT_CONTAINERS",
        "DOCKER_HOST", "DEPENDENT_CONTAINER_FAILURES", "MAX_PARALLEL_CHECKS",
        "AUTO_RECREATE", "LOG_LEVEL", "DNS_WAIT_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)
    assert Config.from_env() == Config()


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setenv("FAIL_THRESHOLD", "4")
    monkeypatch.delenv("DEPENDENT_CONTAINER_FAILURES", raising=False)
    assert Config.from_env().dependent_container_failures == 4


def test_dependent_container_failures_independent_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAIL_THRESHOLD", "4")
    monkeypatch.setenv("DEPENDENT_CONTAINER_FAILURES", "2")
    assert Config.from_env().dependent_container_failures == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("true", True), ("false", False),
     ("yes", True), ("no", False), ("on", True), ("off", False)],
)
def test_auto_recreate_bool_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("AUTO_RECREATE", value)
    assert Config.from_env().auto_recreate is expected


def test_int_fallback_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_INTERVAL", "not-a-number")
    assert Config.from_env().check_interval == 30
