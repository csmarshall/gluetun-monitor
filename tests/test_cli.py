"""CLI prerequisite checks (v1.x ``check_prerequisites`` parity)."""

from __future__ import annotations

import io
from pathlib import Path

from gluetun_monitor.cli import check_prerequisites
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _sites(tmp_path: Path) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    return str(conf)


def test_prereqs_pass(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cfg = Config(config_file=_sites(tmp_path))
    assert check_prerequisites(fake, cfg, _logger()) is True


def test_prereqs_fail_missing_config(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cfg = Config(config_file=str(tmp_path / "absent.conf"))
    assert check_prerequisites(fake, cfg, _logger()) is False


def test_prereqs_fail_docker_unreachable(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.ping_ok = False
    cfg = Config(config_file=_sites(tmp_path))
    assert check_prerequisites(fake, cfg, _logger()) is False


def test_prereqs_fail_gluetun_absent(tmp_path: Path) -> None:
    fake = FakeDockerClient()  # no gluetun container
    cfg = Config(config_file=_sites(tmp_path))
    assert check_prerequisites(fake, cfg, _logger()) is False
