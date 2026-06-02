"""sites.conf is re-read every cycle — add a site to the file, no restart needed.

This is a v1 feature ("just add it to the sites file") that v2 preserves: the
monitor parses the config file on each loop rather than caching it at startup.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def test_added_site_is_picked_up_without_restart(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    tested: list[str] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "wget":
            tested.append(cmd[-1])  # the URL probed inside gluetun
        return ExecResult(0, "")

    fake.on_exec = handler

    conf = tmp_path / "sites.conf"
    conf.write_text("https://a.example\nhttps://b.example\n")
    stream = io.StringIO()
    mon = Monitor(
        fake,
        Config(config_file=str(conf), gluetun_container="gluetun"),
        Logger(log_file=None, level="DEBUG", stream=stream),
        rng=random.Random(0),
        sleep=lambda _s: None,
    )

    mon.run_once()
    assert "Loaded 2 sites" in stream.getvalue()

    # Add a site to the file at runtime — no restart.
    conf.write_text("https://a.example\nhttps://b.example\nhttps://c.example\n")
    tested.clear()
    mon.run_once()

    assert "https://c.example" in tested  # the new site is now tested
    assert "Sites changed: added https://c.example (now 3)" in stream.getvalue()
