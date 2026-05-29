"""An explicitly-listed DEPENDENT_CONTAINERS name that doesn't exist warns loudly."""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _mon(fake: FakeDockerClient, deps: str) -> tuple[Monitor, io.StringIO]:
    stream = io.StringIO()
    cfg = Config(config_file="/dev/null", gluetun_container="gluetun", dependent_containers=deps)
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None), stream


def test_missing_explicit_dependent_warns_and_is_dropped() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    mon, stream = _mon(fake, "ghost")
    assert mon._resolve_dependents() == []  # pruned (doesn't exist)
    assert "Configured dependent 'ghost' not found" in stream.getvalue()


def test_missing_explicit_dependent_warns_only_once() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    mon, stream = _mon(fake, "ghost")
    mon._resolve_dependents()
    mon._resolve_dependents()
    assert stream.getvalue().count("Configured dependent 'ghost' not found") == 1


def test_present_explicit_dependent_does_not_warn() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    mon, stream = _mon(fake, "qbittorrent")
    assert mon._resolve_dependents() == ["qbittorrent"]
    assert "not found" not in stream.getvalue()
