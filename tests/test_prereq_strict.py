"""Strict startup validation: malformed/incomplete config is fatal, never guessed."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from gluetun_monitor import cli
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _sites(tmp_path: Path, body: str) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text(body)
    return str(conf)


def _stack(tmp_path: Path, body: str = "https://www.google.com\n", **cfg: object) -> tuple:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    config = Config(config_file=_sites(tmp_path, body), gluetun_container="gluetun", **cfg)
    return fake, config


# ----- sites.conf must have testable entries -----


def test_prereq_passes_with_sites(tmp_path: Path) -> None:
    fake, config = _stack(tmp_path)
    assert cli.check_prerequisites(fake, config, _logger()) is True


@pytest.mark.parametrize("body", ["", "# only comments\n", "   \n\n"])
def test_prereq_fatal_on_empty_sites(tmp_path: Path, body: str) -> None:
    fake, config = _stack(tmp_path, body)
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    assert cli.check_prerequisites(fake, config, logger) is False
    assert "No testable sites" in stream.getvalue()


def test_prereq_fatal_clean_when_config_is_a_directory(tmp_path: Path) -> None:
    """A CONFIG_FILE that's a directory (the classic missing-bind-mount-source
    that Docker silently turns into a dir) must fail loud and cleanly, not crash
    with an IsADirectoryError traceback (Tenet 7). Found by the upgrade test."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cfg = Config(config_file=str(tmp_path), gluetun_container="gluetun")  # a dir
    stream = io.StringIO()
    assert cli.check_prerequisites(fake, cfg, Logger(log_file=None, stream=stream)) is False
    assert "Cannot read sites config" in stream.getvalue()


# ----- explicit DEPENDENT_CONTAINERS must name existing containers -----


def test_prereq_fatal_on_missing_explicit_dependent(tmp_path: Path) -> None:
    fake, config = _stack(tmp_path, dependent_containers="qbittorrent,ghost")
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")  # ghost absent
    stream = io.StringIO()
    assert cli.check_prerequisites(fake, config, Logger(log_file=None, stream=stream)) is False
    assert "ghost" in stream.getvalue()


def test_prereq_fatal_on_empty_explicit_list(tmp_path: Path) -> None:
    fake, config = _stack(tmp_path, dependent_containers=" , ,")
    assert cli.check_prerequisites(fake, config, _logger()) is False


def test_prereq_passes_with_all_explicit_dependents_present(tmp_path: Path) -> None:
    fake, config = _stack(tmp_path, dependent_containers="qbittorrent")
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    assert cli.check_prerequisites(fake, config, _logger()) is True


def test_prereq_auto_with_no_dependents_is_not_fatal(tmp_path: Path) -> None:
    fake, config = _stack(tmp_path, dependent_containers="auto")
    assert cli.check_prerequisites(fake, config, _logger()) is True


# ----- malformed env is fatal in main() -----


def test_main_fatal_on_malformed_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHECK_INTERVAL", "abc")  # malformed -> Config.errors -> exit 1
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "m.log"))
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "sites.conf"))
    assert cli.main() == 1  # returns before touching Docker
