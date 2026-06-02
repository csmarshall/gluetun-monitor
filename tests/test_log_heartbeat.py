"""The per-loop INFO heartbeat — and the technique for testing log output by level.

Why: the default level is INFO, and a healthy loop must still *say something*
("sites: N/M ok", "dependents: N/M ok") without the per-item DEBUG spam. The way
to test any log-level behavior: point a Logger at a StringIO at the chosen level,
run a loop, and assert what is (and isn't) in the captured text.
"""

from __future__ import annotations

import io
import random

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
SITES = ["https://google.com", "https://cloudflare.com", "https://nzb.su"]


def _run_loop(level: str, *, exec_handler=None, **cfg: object) -> str:
    """Run one monitoring loop at ``level`` and return the captured log text."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("sonarr", network_mode=f"container:{GLUETUN_ID}")

    def healthy(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\ntun0\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = exec_handler or healthy
    stream = io.StringIO()
    logger = Logger(log_file=None, level=level, stream=stream)
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    cfg.setdefault("sites_env", ",".join(SITES))
    mon = Monitor(fake, Config(**cfg), logger, rng=random.Random(0),
                  sleep=lambda _s: None, stats=SiteStatsStore(None))
    mon.run_once()
    return stream.getvalue()


def test_info_shows_heartbeat_not_per_item() -> None:
    out = _run_loop("INFO")
    # Heartbeat summaries ARE present at INFO...
    assert "[gateway:gluetun] sites: 3/3 ok" in out
    assert "dependents: 2/2 ok" in out
    # ...but the per-item detail is NOT (that's DEBUG-only).
    assert "reach ok: https://google.com" not in out
    assert "link live:" not in out


def test_debug_shows_heartbeat_and_per_item() -> None:
    out = _run_loop("DEBUG")
    assert "[gateway:gluetun] sites: 3/3 ok" in out        # heartbeat
    assert "reach ok: https://google.com" in out           # per-site detail
    assert "[dependent:qbittorrent] link live:" in out     # per-dependent detail


def test_heartbeat_reports_failing_site() -> None:
    """A site failing this loop (even below threshold) shows in the INFO summary."""
    def one_bad(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\ntun0\n")
        if "nzb.su" in cmd[-1]:
            return ExecResult(4, "wget: bad address 'nzb.su'\n")  # no HTTP -> fail
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    out = _run_loop("INFO", exec_handler=one_bad)
    assert "[gateway:gluetun] sites: 2/3 ok — failing: nzb.su" in out


def test_heartbeat_reports_dependent_remediation() -> None:
    """A stranded dependent shows as remediating in the INFO summary."""
    def qb_stranded(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "lo\n") if name == "qbittorrent" else ExecResult(0, "eth0\nlo\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    out = _run_loop("INFO", exec_handler=qb_stranded, dependent_containers="qbittorrent,sonarr")
    assert "dependents: 1/2 ok — remediating: qbittorrent" in out


def test_discovery_line_is_debug_only() -> None:
    """The 'Discovered dependent containers' line moved to DEBUG (the heartbeat
    covers it at INFO), so it must not appear at INFO."""
    assert "Discovered dependent containers" not in _run_loop("INFO")
    assert "Discovered dependent containers" in _run_loop("DEBUG")
