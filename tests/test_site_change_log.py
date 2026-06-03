"""Runtime sites.conf edits are logged by *name* (added/removed), including swaps.

Why: sites.conf is re-read every loop, so an operator editing it should see what
changed at INFO — not just "count changed", and especially not silence when a
same-count swap leaves the count unchanged. A removed site also drops its live
failure counter so a later re-add starts clean.
"""

from __future__ import annotations

import io
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore
from gluetun_monitor.state import Counter

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
A, B, C, D, E = (f"https://{x}.example" for x in "abcde")


def _write(conf: Path, sites: list[str]) -> None:
    conf.write_text("\n".join(sites) + "\n", encoding="utf-8")


def _monitor(tmp_path: Path) -> tuple[Monitor, io.StringIO, FakeDockerClient, Path]:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def healthy(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "wget":
            return ExecResult(0, "  HTTP/1.1 200 OK\n")
        return ExecResult(0, "eth0\nlo\n")

    fake.on_exec = healthy
    stream = io.StringIO()
    logger = Logger(log_file=None, level="INFO", stream=stream)
    conf = tmp_path / "sites.conf"
    # Manual empty dependent set -> dependent phase no-ops, keeping output focused.
    mon = Monitor(fake, Config(config_file=str(conf), gluetun_container="gluetun",
                               dependent_containers=""),
                  logger, sleep=lambda _s: None, stats=SiteStatsStore(None))
    return mon, stream, fake, conf


def _loop(mon: Monitor, conf: Path, sites: list[str]) -> None:
    _write(conf, sites)
    mon.run_once()


def test_first_load_logs_count(tmp_path: Path) -> None:
    mon, stream, _, conf = _monitor(tmp_path)
    _loop(mon, conf, [A, B, C])
    assert "Loaded 3 sites" in stream.getvalue()


def test_add_logs_added_name(tmp_path: Path) -> None:
    mon, stream, _, conf = _monitor(tmp_path)
    _loop(mon, conf, [A, B, C])
    _loop(mon, conf, [A, B, C, D])
    assert f"Sites changed: added {D} (now 4)" in stream.getvalue()


def test_remove_logs_removed_name(tmp_path: Path) -> None:
    mon, stream, _, conf = _monitor(tmp_path)
    _loop(mon, conf, [A, B, C])
    _loop(mon, conf, [A, B])
    assert f"Sites changed: removed {C} (now 2)" in stream.getvalue()


def test_swap_same_count_is_logged(tmp_path: Path) -> None:
    """The blind spot of count-only tracking: count stays 3, but the set changed."""
    mon, stream, _, conf = _monitor(tmp_path)
    _loop(mon, conf, [A, B, C])
    _loop(mon, conf, [A, B, E])  # swap C -> E, still 3
    assert f"Sites changed: added {E}; removed {C} (now 3)" in stream.getvalue()


def test_no_change_logs_nothing(tmp_path: Path) -> None:
    mon, stream, _, conf = _monitor(tmp_path)
    _loop(mon, conf, [A, B, C])
    before = len(stream.getvalue())
    _loop(mon, conf, [A, B, C])  # identical sites.conf
    assert "Sites changed" not in stream.getvalue()[before:]


def test_removed_site_failure_counter_discarded(tmp_path: Path) -> None:
    """A site failing below threshold, then removed, must not carry its count back
    if re-added."""
    mon, _stream, fake, conf = _monitor(tmp_path)

    def b_fails(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "wget" and B in cmd[-1]:
            return ExecResult(4, "wget: bad address\n")  # no HTTP -> fail
        if cmd and cmd[0] == "wget":
            return ExecResult(0, "  HTTP/1.1 200 OK\n")
        return ExecResult(0, "eth0\nlo\n")

    fake.on_exec = b_fails
    _loop(mon, conf, [A, B, C])
    assert mon.site_failures.get(B) == 1  # B failed once (below threshold 2)

    _loop(mon, conf, [A, C])  # remove B
    assert mon.site_failures.get(B) == 0  # counter discarded, not lingering at 1


def test_counter_discard() -> None:
    c = Counter()
    c.fail("x")
    c.fail("x")
    assert c.get("x") == 2
    c.discard("x")
    assert c.get("x") == 0  # forgotten
    c.discard("never-seen")  # no error
