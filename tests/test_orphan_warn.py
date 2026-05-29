"""A2: warn (don't recreate) about a running container on a dead netns parent.

Why: a dependent recreate-stranded *before* the monitor started points at
gluetun's old, now-gone id, so current-id discovery can't see it. We surface it
with an actionable WARN suggesting DEPENDENT_CONTAINERS — but we deliberately do
NOT auto-recreate it, because an orphan whose parent is gone can't be confirmed
as *this* gluetun's dependent (Tenet 1, first do no harm).
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
DEAD_ID = "d" * 64


def _mon(fake: FakeDockerClient, **cfg: object) -> tuple[Monitor, io.StringIO]:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return Monitor(fake, Config(**cfg), logger, rng=random.Random(0), sleep=lambda _s: None), stream


def test_warns_about_dangling_orphan_but_does_not_recreate() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    # 'orphan' points at a container id that doesn't exist (dead netns parent).
    fake.add_container("orphan", network_mode=f"container:{DEAD_ID}")
    mon, stream = _mon(fake)
    mon._warn_dangling_orphans()
    out = stream.getvalue()
    assert "orphan" in out and "no longer exists" in out
    assert "DEPENDENT_CONTAINERS" in out
    assert fake.created == [] and fake.removed == []  # warned, not acted on


def test_no_warn_when_netns_parent_exists() -> None:
    """A dependent on gluetun's *current* (live) id is healthy, not an orphan."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon, stream = _mon(fake)
    mon._warn_dangling_orphans()
    assert "no longer exists" not in stream.getvalue()


def test_no_warn_for_excluded_or_listed() -> None:
    """An orphan the operator excluded (or explicitly listed) isn't warned about —
    we either won't touch it or already manage it."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("orphan", network_mode=f"container:{DEAD_ID}")
    mon, stream = _mon(fake, exclude_containers="orphan")
    mon._warn_dangling_orphans()
    assert "no longer exists" not in stream.getvalue()


def test_orphan_warning_dedups() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("orphan", network_mode=f"container:{DEAD_ID}")
    mon, stream = _mon(fake)
    mon._warn_dangling_orphans()
    mon._warn_dangling_orphans()
    assert stream.getvalue().count("no longer exists") == 1
