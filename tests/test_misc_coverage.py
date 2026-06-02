"""Small, direct tests for helper branches not exercised by higher-level tests:
the _env_int upper-bound path, Logger.log, the empty-percentile guard, the
unsafe-site startup warning, and the DRY_RUN banner.
"""

from __future__ import annotations

import io
from pathlib import Path

from gluetun_monitor.cli import _announce_banner, check_prerequisites
from gluetun_monitor.config import Config, _env_int
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.site_stats import _percentile

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(log_file=None, level="DEBUG", stream=stream), stream


# ----- _env_int upper bound (config.py:42-43) -----

def test_env_int_rejects_above_maximum(monkeypatch) -> None:
    monkeypatch.setenv("X", "11")
    errors: list[str] = []
    assert _env_int("X", 5, errors, minimum=0, maximum=10) == 5  # falls back to default
    assert errors and "must be <= 10" in errors[0]


def test_env_int_accepts_at_maximum(monkeypatch) -> None:
    monkeypatch.setenv("X", "10")
    errors: list[str] = []
    assert _env_int("X", 5, errors, maximum=10) == 10
    assert errors == []


# ----- Logger.log generic dispatch (logging_setup.py:113) -----

def test_logger_log_dispatches_named_level() -> None:
    logger, stream = _logger()
    logger.log("CHECK", "hello-check")
    assert "hello-check" in stream.getvalue()


# ----- empty percentile guard (site_stats.py:53) -----

def test_percentile_of_empty_is_zero() -> None:
    assert _percentile([], 50) == 0
    assert _percentile([7], 99) == 7


# ----- unsafe-site startup warning + still starts (cli.py:37) -----

def test_prereqs_warn_on_unsafe_site_but_start(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://ok.example\n--evil-flag\n", encoding="utf-8")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    logger, stream = _logger()
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    assert check_prerequisites(fake, cfg, logger) is True  # one good site remains
    assert "Ignoring unsafe site entry '--evil-flag'" in stream.getvalue()


def test_prereqs_fail_when_only_site_is_unsafe(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("--evil-flag\n", encoding="utf-8")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    logger, stream = _logger()
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    assert check_prerequisites(fake, cfg, logger) is False  # nothing safe left to test
    assert "No testable sites" in stream.getvalue()


# ----- DRY_RUN banner (cli.py:119) -----

def test_announce_banner_warns_on_dry_run() -> None:
    logger, stream = _logger()
    _announce_banner(Config(config_file="/dev/null", dry_run=True), logger)
    assert "DRY_RUN enabled" in stream.getvalue()
