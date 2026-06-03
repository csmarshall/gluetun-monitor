"""SITES env var: CSV test URLs, unioned + deduped with sites.conf."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from gluetun_monitor import cli
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.sites import load_sites, parse_sites_csv

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_parse_sites_csv() -> None:
    assert parse_sites_csv("https://a, https://b ,, https://c") == [
        "https://a", "https://b", "https://c"
    ]
    assert parse_sites_csv("") == []


def test_load_sites_file_only(tmp_path: Path) -> None:
    conf = tmp_path / "s.conf"
    conf.write_text("https://a\nhttps://b\n")
    assert load_sites(str(conf), None) == ["https://a", "https://b"]


def test_load_sites_env_only_missing_file(tmp_path: Path) -> None:
    # No file present -> file contributes nothing; env supplies the set.
    assert load_sites(str(tmp_path / "absent.conf"), "https://x,https://y") == [
        "https://x", "https://y"
    ]


def test_load_sites_union_dedup(tmp_path: Path) -> None:
    conf = tmp_path / "s.conf"
    conf.write_text("https://a\nhttps://b\n")
    # 'https://b' is in both -> deduped; file order first, then env-only.
    assert load_sites(str(conf), "https://b,https://c") == [
        "https://a", "https://b", "https://c"
    ]


def test_load_sites_empty_when_neither(tmp_path: Path) -> None:
    assert load_sites(str(tmp_path / "absent.conf"), None) == []


# ----- prereq + end-to-end -----


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def test_prereq_passes_with_sites_env_and_no_file(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    config = Config(
        config_file=str(tmp_path / "absent.conf"),  # no file mounted
        gluetun_container="gluetun",
        sites_env="https://only-env.example",
    )
    assert cli.check_prerequisites(fake, config, _logger()) is True


def test_prereq_fatal_when_no_file_and_no_sites_env(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    config = Config(config_file=str(tmp_path / "absent.conf"), gluetun_container="gluetun")
    stream = io.StringIO()
    assert cli.check_prerequisites(fake, config, Logger(log_file=None, stream=stream)) is False
    assert "No testable sites" in stream.getvalue()


def test_from_env_reads_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITES", "https://a,https://b")
    assert Config.from_env().sites_env == "https://a,https://b"
