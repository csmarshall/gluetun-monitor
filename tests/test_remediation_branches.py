"""Failure-path coverage for remediation decisions (recovery + recreate).

These are the load-bearing "things went wrong" branches: a restart that raises, an
escalation that also fails, a recreate whose spec can't be built, and verifying a
container that vanished. They must fail closed (return False, log loudly) and
never crash the loop.
"""

from __future__ import annotations

import io
from typing import Any

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recovery import remediate_dependent, verify_dependent
from gluetun_monitor.recreate import recreate_dependent

from .fakes import FakeDockerClient, make_inspect

GLUETUN_ID = "a" * 64
OLD_ID = "b" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _cfg(**kw: object) -> Config:
    kw.setdefault("config_file", "/dev/null")
    kw.setdefault("gluetun_container", "gluetun")
    return Config(**kw)


class _RestartRaises(FakeDockerClient):
    """A daemon where restart always errors (e.g. container wedged)."""

    def restart(self, name_or_id: str) -> None:
        raise RuntimeError("simulated restart failure")


# ----- verify_dependent (recovery.py:133) -----

def test_verify_dependent_false_when_missing() -> None:
    fake = FakeDockerClient()
    assert verify_dependent(fake, "ghost") is False


def test_verify_dependent_false_when_not_running() -> None:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}", running=False)
    assert verify_dependent(fake, "dep") is False


# ----- remediate_dependent restart-fails (recovery.py:183) -----

def test_remediate_restart_failure_returns_false() -> None:
    fake = _RestartRaises()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")  # same id -> RESTART
    fake.on_exec = lambda n, c: ExecResult(1, "")
    assert remediate_dependent(fake, "dep", GLUETUN_ID, _cfg(), _logger(),
                               sleep=lambda _s: None) is False


# ----- TRY_RESTART escalates, recreate disabled -> fails (recovery.py:188-189) -----

def test_remediate_nameform_restart_fail_escalates_to_disabled_recreate() -> None:
    fake = _RestartRaises()
    fake.add_container("dep", network_mode="container:gluetun")  # name form -> TRY_RESTART
    assert remediate_dependent(fake, "dep", GLUETUN_ID, _cfg(auto_recreate=False),
                               _logger(), sleep=lambda _s: None) is False


def test_remediate_missing_container_returns_false() -> None:
    fake = FakeDockerClient()
    assert remediate_dependent(fake, "ghost", GLUETUN_ID, _cfg(), _logger(),
                               sleep=lambda _s: None) is False


# ----- recreate spec-build failure (recreate.py:135-137) -----

def test_recreate_build_spec_failure_returns_false() -> None:
    """A malformed inspect payload (Config is a list) makes build_create_body
    raise; recreate must catch it and fail closed WITHOUT removing the container."""
    fake = FakeDockerClient()
    raw: dict[str, Any] = make_inspect("dep", id="c" * 64,
                                       network_mode=f"container:{OLD_ID}")
    raw["Config"] = ["not", "a", "dict"]  # build_create_body will choke on .pop()
    fake.add(raw)
    assert recreate_dependent(fake, "dep", GLUETUN_ID, _logger()) is False
    assert fake.removed == []  # fail-closed: never removed the container


class _CreateRaises(FakeDockerClient):
    def create_from_config(self, config: dict[str, Any], name: str) -> str:
        raise RuntimeError("simulated create failure")


def test_recreate_create_failure_after_remove_returns_false() -> None:
    """If create fails after the remove, recreate returns False (the dependent is
    left removed — documented, hard to make atomic given Docker name reuse)."""
    fake = _CreateRaises()
    fake.add_container("dep", network_mode=f"container:{OLD_ID}")
    assert recreate_dependent(fake, "dep", GLUETUN_ID, _logger()) is False
    assert fake.removed == [("dep", False)]  # remove happened, with volumes preserved
