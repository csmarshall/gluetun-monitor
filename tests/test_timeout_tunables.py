"""TIMEOUT + WGET_TRIES are standardized across every probe method.

Why: an operator setting TIMEOUT=10 expects it to reach the wget inside the
dependent containers and the gluetun site tests and the post-restart DNS probe —
one knob, everywhere. WGET_TRIES likewise. These tests assert the flags actually
land on the exec'd commands.
"""

from __future__ import annotations

import io

from gluetun_monitor.config import Config
from gluetun_monitor.connectivity import probe_site
from gluetun_monitor.dns_check import validate_dns
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recovery import wait_for_dns

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _capture() -> tuple[FakeDockerClient, list[list[str]]]:
    fake = FakeDockerClient()
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    return fake, cmds


def test_probe_site_applies_timeout_and_tries() -> None:
    fake, cmds = _capture()
    probe_site(fake, "dep", "https://x", timeout=10, tries=3)
    assert "--timeout=10" in cmds[0]
    assert "--tries=3" in cmds[0]


def test_validate_dns_threads_timeout_and_tries_to_wget() -> None:
    """The same TIMEOUT/WGET_TRIES reach the dependent-container wget."""
    fake, cmds = _capture()
    validate_dns(fake, "qbittorrent", "https://x", "x", timeout=15, tries=2)
    wget_cmds = [c for c in cmds if c and c[0] == "wget"]
    assert wget_cmds and "--timeout=15" in wget_cmds[0] and "--tries=2" in wget_cmds[0]


def test_config_default_timeout_and_tries() -> None:
    c = Config()
    assert c.timeout == 10
    assert c.wget_tries == 1


def test_from_env_wget_tries(monkeypatch) -> None:
    monkeypatch.setenv("WGET_TRIES", "4")
    assert Config.from_env().wget_tries == 4


def test_wait_for_dns_uses_standardized_timeout_not_hardcoded() -> None:
    """The post-restart DNS probe uses request_timeout/tries (no hardcoded 5)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(0, "")  # nslookup + wget both succeed -> returns fast

    fake.on_exec = handler
    logger = Logger(log_file=None, stream=io.StringIO())
    assert wait_for_dns(fake, "gluetun", 30, logger, request_timeout=10, tries=2,
                        sleep=lambda _s: None) is True
    wget_cmds = [c for c in cmds if c and c[0] == "wget"]
    assert wget_cmds and "--timeout=10" in wget_cmds[0] and "--tries=2" in wget_cmds[0]
    assert all("--timeout=5" not in c for c in wget_cmds)  # no hardcoded 5
