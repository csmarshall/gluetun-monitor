"""End-to-end: a chronically-flaky site triggers the flaky-site advisory.

Why: ties the gluetun root test → restart attribution → advisory together, the
way it runs live. A site that keeps causing restarts should, after enough of
them, produce the "FLAKY SITE … review it" warning (the human-in-the-loop
escalation chosen over auto-quarantine).
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_repeated_restarts_from_one_site_raise_advisory(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://flaky.example\n")

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        # gluetun's DNS-stability probe passes (fast wait); the test site always
        # fails to respond -> breaches every loop -> triggers a restart each loop.
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        if "https://1.1.1.1" in cmd:
            return ExecResult(0, "")
        return ExecResult(4, "")  # flaky.example never responds

    fake.on_exec = handler

    stream = io.StringIO()
    cfg = Config(
        config_file=str(conf),
        gluetun_container="gluetun",
        fail_threshold=1,            # breach on first failure each loop
        dns_wait_timeout=2,
        advisory_min_restarts=3,     # advise after 3 restarts
        advisory_dominance=0.5,       # don't write during the test
    )
    mon = Monitor(
        fake, cfg, Logger(log_file=None, stream=stream),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None),  # in-memory, no filesystem
    )

    mon.run_once()  # restart 1
    mon.run_once()  # restart 2
    assert "FLAKY SITE" not in stream.getvalue()
    mon.run_once()  # restart 3 -> advisory fires
    out = stream.getvalue()
    assert "FLAKY SITE: https://flaky.example" in out
    assert "review" in out
    # The site's restart attribution is recorded.
    assert mon.stats.sites["https://flaky.example"].restarts_triggered == 3


def test_advisory_deduped_not_per_loop(tmp_path: Path) -> None:
    """Once warned about a site, don't repeat every loop (no spam)."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://flaky.example\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = lambda n, c: (
        ExecResult(0, "") if (c and c[0] == "nslookup") or "https://1.1.1.1" in c
        else ExecResult(4, "")
    )
    stream = io.StringIO()
    cfg = Config(config_file=str(conf), gluetun_container="gluetun", fail_threshold=1,
                 dns_wait_timeout=2, advisory_min_restarts=3, advisory_dominance=0.5)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=stream),
                  rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None))
    for _ in range(6):
        mon.run_once()
    assert stream.getvalue().count("FLAKY SITE: https://flaky.example") == 1
