"""CLI prerequisite checks (v1.x ``check_prerequisites`` parity)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from gluetun_monitor.cli import (
    _log_notify_summary,
    _run_suggest_tunables,
    check_prerequisites,
)
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.site_stats import SiteStatsStore

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


def test_notify_summary_disabled() -> None:
    stream = io.StringIO()
    _log_notify_summary(Config(), Logger(log_file=None, level="INFO", stream=stream))
    assert "disabled" in stream.getvalue()


def test_notify_summary_enabled_masks_urls() -> None:
    stream = io.StringIO()
    cfg = Config(
        apprise_urls=("ntfy://super-secret-token@host/topic",),
        notify_level="attention",
        notify_repeat_interval=0,
    )
    _log_notify_summary(cfg, Logger(log_file=None, level="INFO", stream=stream))
    out = stream.getvalue()
    assert "ENABLED" in out and "ntfy" in out  # scheme shown
    assert "super-secret-token" not in out  # but never the URL/token
    assert "announced once" in out  # repeat=0 behavior is stated


# ----- --suggest-tunables doctor (#60) -----

def _stats_with_slow_site(path: str) -> None:
    """Persist a stats file whose one site is slow-but-alive (read-timeouts plus
    successful responses over the ceiling) so the doctor has a clear suggestion."""
    store = SiteStatsStore(path)
    for _ in range(4):
        store.record_poll("https://slow.example", False, 0, "Operation timed out")
    store.record_poll("https://slow.example", True, 13000, "")
    for _ in range(8):
        store.record_restart("https://slow.example")
    assert store.save()


def test_suggest_tunables_prints_paste_ready_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stats = str(tmp_path / "site-stats.json")
    _stats_with_slow_site(stats)
    cfg = Config(stats_file=stats, timeout=10, wget_tries=1)
    assert _run_suggest_tunables(cfg, _logger()) == 0
    out = capsys.readouterr().out
    assert "slow.example" in out
    assert "https://slow.example|timeout=" in out  # the paste-ready override


def test_suggest_tunables_no_stats_is_graceful(tmp_path: Path) -> None:
    """No stats file yet → a friendly message and a clean exit, not a crash."""
    cfg = Config(stats_file=str(tmp_path / "absent.json"))
    assert _run_suggest_tunables(cfg, _logger()) == 0
