"""#110: per-site |role= governs whether a site's failure gates a gluetun restart.

- ``critical`` (default, bare URL) restarts on failure — today's behavior.
- ``advisory`` is still probed and recorded for reachability, but NEVER restarts,
  so a site blocked through every exit (geo-blocked/anti-VPN) can't roll the tunnel.
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
from gluetun_monitor.sites import parse_entry

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
GOOD = "https://1.1.1.1"
BAD = "https://blocked.example"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_bare_url_defaults_to_critical() -> None:
    spec, warnings = parse_entry("https://example.com")
    assert spec is not None
    assert spec.role == "critical"
    assert warnings == []


def test_role_advisory_parsed() -> None:
    spec, warnings = parse_entry("https://example.com|role=advisory")
    assert spec is not None and spec.role == "advisory"
    assert warnings == []


def test_role_is_case_insensitive() -> None:
    spec, _ = parse_entry("https://example.com|role=ADVISORY")
    assert spec is not None and spec.role == "advisory"


def test_unknown_role_warns_and_defaults_critical() -> None:
    spec, warnings = parse_entry("https://example.com|role=bogus")
    assert spec is not None and spec.role == "critical"
    assert any("unknown role" in w and "bogus" in w for w in warnings)


def test_role_composes_with_int_options() -> None:
    spec, warnings = parse_entry("https://slow.example|role=advisory|timeout=25|tries=2")
    assert spec is not None
    assert (spec.role, spec.timeout, spec.tries) == ("advisory", 25, 2)
    assert warnings == []


# --------------------------------------------------------------------------- #
# Loop behavior
# --------------------------------------------------------------------------- #

def _mon(tmp_path: Path, conf_text: str, handler, **cfg_over):
    conf = tmp_path / "sites.conf"
    conf.write_text(conf_text)
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = handler
    cfg = Config(
        config_file=str(conf), gluetun_container="gluetun",
        fail_threshold=1, dns_wait_timeout=2, advisory_min_restarts=1000, **cfg_over,
    )
    mon = Monitor(
        fake, cfg, Logger(log_file=None, stream=io.StringIO()),
        rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None),
    )
    return mon, fake


def _handler(fail_urls: set[str]):
    def h(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        for u in fail_urls:
            if u in cmd:
                return ExecResult(4, "")
        return ExecResult(0, "")
    return h


def test_advisory_failure_never_restarts_but_is_recorded(tmp_path: Path) -> None:
    mon, fake = _mon(tmp_path, f"{GOOD}\n{BAD}|role=advisory\n", _handler({BAD}))
    for _ in range(3):
        mon.run_once()
    # The advisory site failed every loop, but the tunnel was never restarted.
    assert fake.restarted == []
    # ...and it was still probed + recorded (reachability observability).
    assert BAD in mon.stats.sites
    assert mon.stats.sites[BAD].total_failures >= 3


def test_critical_default_still_restarts(tmp_path: Path) -> None:
    """Backward-compat: a bare URL is critical, so its failure restarts as before."""
    mon, fake = _mon(tmp_path, f"{BAD}\n", _handler({BAD}))
    mon.run_once()
    assert "gluetun" in fake.restarted


def test_critical_gates_even_when_an_advisory_site_also_fails(tmp_path: Path) -> None:
    mon, fake = _mon(tmp_path, f"{GOOD}|role=critical\n{BAD}|role=advisory\n",
                     _handler({GOOD, BAD}))
    mon.run_once()
    # The critical site failing triggers the restart; the advisory one is irrelevant.
    assert "gluetun" in fake.restarted


def test_advisory_failure_absent_from_restart_but_shown_in_heartbeat(tmp_path: Path) -> None:
    stream = io.StringIO()
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{BAD}|role=advisory\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = _handler({BAD})
    cfg = Config(config_file=str(conf), gluetun_container="gluetun",
                 fail_threshold=1, dns_wait_timeout=2, advisory_min_restarts=1000)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=stream, level="DEBUG"),
                  rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None))
    mon.run_once()
    out = stream.getvalue()
    assert "advisory — not gating" in out          # per-site debug line
    assert "(advisory)" in out                      # annotated in the heartbeat
    assert "→ restart" not in out                   # never proposed a restart


def test_startup_debug_enumerates_each_site_resolved_config(tmp_path: Path) -> None:
    """Startup DEBUG lists every site with its fully-resolved config — role plus
    effective timeout/tries, defaults folded in — including all-default sites."""
    stream = io.StringIO()
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{BAD}|role=advisory|timeout=25\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = _handler(set())  # everything passes; we only assert startup lines
    cfg = Config(config_file=str(conf), gluetun_container="gluetun",
                 fail_threshold=1, dns_wait_timeout=2, timeout=10, wget_tries=1)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=stream, level="DEBUG"),
                  rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None))
    mon.run_once()
    out = stream.getvalue()
    assert f"site {GOOD} [role=critical timeout=10s tries=1]" in out   # all-default site
    assert f"site {BAD} [role=advisory timeout=25s tries=1]" in out    # overridden site
