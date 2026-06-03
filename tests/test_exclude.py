"""EXCLUDE_CONTAINERS denylist — containers the monitor must never manage.

Why this exists: auto-discovery is all-or-nothing, so without a denylist a user
who wants "discover everything except this one" would have to abandon auto and
hand-maintain a full list. EXCLUDE is the escape hatch, and its overlap handling
encodes the "first, do no harm" stance — when include and exclude contradict, we
take the non-destructive action (exclude) and warn, rather than guess or restart.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor import cli
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _mon(fake: FakeDockerClient, **cfg: object) -> tuple[Monitor, io.StringIO]:
    stream = io.StringIO()
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    config = Config(**cfg)
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return Monitor(fake, config, logger, rng=random.Random(0), sleep=lambda _s: None), stream


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def test_excluded_container_dropped_from_auto_discovery() -> None:
    """An auto-discovered dependent named in EXCLUDE must not appear in the managed
    set — proving exclude filters discovery, not just explicit lists."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep1", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("dep2", network_mode=f"container:{GLUETUN_ID}")
    mon, _ = _mon(fake, exclude_containers="dep2")
    assert mon._resolve_dependents() == ["dep1"]  # dep2 excluded


def test_exclude_subtracts_from_explicit_list() -> None:
    """Exclude also subtracts from an explicit DEPENDENT_CONTAINERS list, so the
    denylist works regardless of how a dependent was selected."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep1", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("dep2", network_mode=f"container:{GLUETUN_ID}")
    mon, _ = _mon(fake, dependent_containers="dep1,dep2", exclude_containers="dep2")
    assert mon._resolve_dependents() == ["dep1"]


def test_overlap_excludes_and_warns_first_do_no_harm() -> None:
    """A container named in BOTH lists is a contradiction; we take the safe action
    (exclude it) and WARN rather than fail or manage it — "first, do no harm"."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("qbit", network_mode=f"container:{GLUETUN_ID}")
    config = Config(
        config_file="/dev/null", gluetun_container="gluetun",
        sites_env="https://x", dependent_containers="qbit", exclude_containers="qbit",
    )
    stream = io.StringIO()
    # Not fatal: prereqs pass (it just means gluetun-only).
    assert cli.check_prerequisites(fake, config, Logger(log_file=None, stream=stream)) is True
    out = stream.getvalue()
    assert "both DEPENDENT_CONTAINERS and EXCLUDE_CONTAINERS" in out
    assert "first, do no harm" in out


def test_unmatched_exclude_name_warns_as_likely_typo() -> None:
    """An exclude name matching no container is usually a typo — and a dangerous
    one (the container you meant to protect would still be managed) — so we WARN.
    Not fatal: it may legitimately not exist yet."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    config = Config(
        config_file="/dev/null", gluetun_container="gluetun",
        sites_env="https://x", exclude_containers="ghost",
    )
    stream = io.StringIO()
    assert cli.check_prerequisites(fake, config, Logger(log_file=None, stream=stream)) is True
    assert "EXCLUDE_CONTAINERS names container(s) not found: ghost" in stream.getvalue()


def test_announce_logs_exclusions() -> None:
    """Startup logging surfaces the denylist so an operator can confirm what is
    deliberately unmanaged."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    mon, stream = _mon(fake, exclude_containers="dep2,dep3")
    mon.announce()
    assert "Excluded from management (EXCLUDE_CONTAINERS): dep2,dep3" in stream.getvalue()
