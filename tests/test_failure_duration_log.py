"""A failing probe logs how long it took — the only thing that separates a hard
failure from a timeout.

`wget` exit 4 is `"Network failure (DNS or connection)"`, and it covers BOTH an
instantly-refused connection (or NXDOMAIN) AND a probe that hung until `--timeout`
expired. There is no distinct timeout exit code, so the reason string can never tell
those apart — the duration is the only signal that can. It was logged on success and
dropped on failure, which is backwards: on a passing probe the duration is trivia, and
on a failing one it is the diagnosis.

`87ms` = the site refused us instantly. `15003ms` = it hung out the entire budget we
gave it. Same reason string; completely different faults, and different fixes (raising
the timeout does nothing for the first).
"""

from __future__ import annotations

import io
import random
import re
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64
DEAD = "http://dead.example"


def _run(tmp_path: Path, sites: str, level: str = "DEBUG") -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text(sites)
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    log = io.StringIO()

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["ls"]:
            return ExecResult(0, "eth0 lo tun0\n")
        if cmd[:1] in (["nslookup"], ["getent"]):
            return ExecResult(0, "1.1.1.1\n")
        if any(DEAD in str(c) for c in cmd):
            return ExecResult(4, "wget: bad address 'dead.example'\n")  # exit 4
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    mon = Monitor(
        fake,
        Config(config_file=str(conf), gluetun_container="gluetun",
               fail_threshold=2, dns_wait_timeout=0, advisory_min_restarts=999),
        Logger(log_file=None, stream=log, level=level),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=FakeNotifier(),
    )
    mon.run_once()
    return log.getvalue()


def _fail_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "reach fail" in ln]


def test_sub_threshold_failure_logs_its_duration(tmp_path: Path) -> None:
    out = _run(tmp_path, f"{DEAD}\n")
    lines = _fail_lines(out)
    assert lines, out
    assert DEAD in lines[0]
    # reason AND duration, together, in one parenthetical — the reason alone is ambiguous.
    assert re.search(r"\(.+, \d+ms\)", lines[0]), (
        f"no duration — a hard failure and a timed-out one read identically: {lines[0]}"
    )


def test_advisory_failure_logs_its_duration(tmp_path: Path) -> None:
    """The advisory line is where this bites hardest: an advisory site never gates a
    restart, so the log IS the whole signal about it."""
    out = _run(tmp_path, f"{DEAD}|role=advisory|timeout=15\n")
    lines = _fail_lines(out)
    assert lines, out
    assert "advisory — not gating" in lines[0]
    assert re.search(r"\(.+, \d+ms\)", lines[0]), f"no duration on the advisory line: {lines[0]}"


def test_gating_failure_logs_its_duration(tmp_path: Path) -> None:
    """The WARN line that precedes a tunnel restart — the one an operator reads first."""
    out = _run(tmp_path, f"{DEAD}\n")  # fail_threshold=2
    out += _run(tmp_path, f"{DEAD}\n")  # (fresh monitor; assert on the shape, not the count)
    restart_lines = [ln for ln in _fail_lines(out) if "→ restart" in ln or "/2]" in ln]
    assert restart_lines, out
    assert all(re.search(r"\(.+, \d+ms\)", ln) for ln in restart_lines), restart_lines
