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
    # The defaults summary spells out the global defaults, role included.
    assert "WGET_TRIES=1, role=critical" in out


def test_advisory_site_excluded_from_dependent_viability_pool(tmp_path: Path) -> None:
    """An advisory site must not be probed by DEPENDENTS either (#110): a dependent
    can't be judged unhealthy for failing to reach a non-gating site. With
    samples=-1 a dependent probes ALL resolvable sites, so without the exclusion it
    would hit the advisory one — this pins that it never does, while the gateway
    still probes it for stats."""
    goodhost, advhost = "https://good.example", "https://watch.example"
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{goodhost}\n{advhost}|role=advisory\n")

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    calls: list[tuple[str, str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\ntun0\n")  # LIVE, not stranded
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        if cmd and cmd[0] == "wget":
            calls.append((name, cmd[-1]))
            return ExecResult(0, "  HTTP/1.1 200 OK\n") if goodhost in cmd[-1] else ExecResult(4, "")
        return ExecResult(0, "")

    fake.on_exec = handler
    cfg = Config(config_file=str(conf), gluetun_container="gluetun",
                 dependent_containers="dep", fail_threshold=1,
                 dependent_viability_samples=-1)  # sample ALL -> would hit advisory w/o the fix
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None))
    mon.run_once()

    dep_targets = [url for n, url in calls if n == "dep"]
    gw_targets = [url for n, url in calls if n == "gluetun"]
    assert any(advhost in t for t in gw_targets)          # gateway still probes it (stats)
    assert all(advhost not in t for t in dep_targets)     # dependents never do
    assert any(goodhost in t for t in dep_targets)        # but they do probe the critical site


def test_flaky_advisory_suppressed_once_site_is_role_advisory(tmp_path: Path) -> None:
    """#110 (review finding): the flaky-site advisory must NOT keep paging about a
    site the operator already switched to role=advisory — even though its prior
    restarts still sit in the dominance window. A dominant CRITICAL site still
    reports (positive control)."""
    from types import SimpleNamespace

    from gluetun_monitor.sites import load_specs

    advurl, goodurl = "https://blocked.example", "https://good.example"
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{goodurl}\n{advurl}|role=advisory\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = _handler(set())
    mon = Monitor(fake, Config(config_file=str(conf), gluetun_container="gluetun"),
                  Logger(log_file=None, stream=io.StringIO()), rng=random.Random(0),
                  sleep=lambda _s: None, stats=SiteStatsStore(None))
    mon._specs = {s.url: s for s in load_specs(str(conf), None)}

    # Advisory-role site dominates the window (prior critical-era churn) -> suppressed.
    mon.stats.advisory = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
        site=advurl, site_restarts=9, total_restarts=10, window_seconds=3600)
    mon.alerts.begin_loop()
    mon._emit_advisory()
    assert f"advisory:{advurl}" not in mon.alerts._reported

    # Positive control: a dominant CRITICAL site still fires the advisory.
    mon.stats.advisory = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
        site=goodurl, site_restarts=9, total_restarts=10, window_seconds=3600)
    mon.alerts.begin_loop()
    mon._emit_advisory()
    assert f"advisory:{goodurl}" in mon.alerts._reported


def test_role_switch_advisory_to_critical_resets_failure_grace(tmp_path: Path) -> None:
    """#110 review: an advisory site accumulates failures without gating. Flipping it
    to critical must NOT inherit that count and restart grace-lessly — the counter is
    cleared on the role change so it gets a fresh FAIL_THRESHOLD grace."""
    badurl = "https://blocked.example"
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{badurl}|role=advisory\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.on_exec = _handler({badurl})
    cfg = Config(config_file=str(conf), gluetun_container="gluetun",
                 fail_threshold=2, dns_wait_timeout=2, advisory_min_restarts=1000)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None, stats=SiteStatsStore(None))

    for _ in range(4):
        mon.run_once()  # advisory: fails every loop, accumulates count, never restarts
    assert fake.restarted == []
    assert mon.site_failures.get(badurl) >= 3  # carried a high count while advisory

    conf.write_text(f"{GOOD}\n{badurl}\n")  # switch it to critical (bare URL)
    mon.run_once()  # counter reset on the switch -> this is only 1/2, NO restart yet
    assert fake.restarted == []
    mon.run_once()  # now 2/2 -> restart
    assert "gluetun" in fake.restarted


def test_advisory_down_alert_is_opt_in_activity_edge_triggered(tmp_path: Path) -> None:
    """An advisory site's unreachability surfaces as an opt-in `activity`-tier alert:
    announced once after FAIL_THRESHOLD, not re-announced while it persists, and
    resolved when it recovers. Never gates a restart."""
    from .fakes import FakeNotifier

    badurl = "https://blocked.example"
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{GOOD}\n{badurl}|role=advisory\n")
    state = {"block": True}
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd and cmd[0] == "nslookup":
            return ExecResult(0, "")
        if badurl in cmd and state["block"]:
            return ExecResult(4, "")
        return ExecResult(0, "")

    fake.on_exec = handler
    notifier = FakeNotifier()
    cfg = Config(config_file=str(conf), gluetun_container="gluetun", fail_threshold=2,
                 dns_wait_timeout=2, advisory_min_restarts=1000)
    mon = Monitor(fake, cfg, Logger(log_file=None, stream=io.StringIO()),
                  rng=random.Random(0), sleep=lambda _s: None,
                  stats=SiteStatsStore(None), notifier=notifier)
    key = f"advisory-down:{badurl}"

    mon.run_once()  # count 1/2 -> not yet announced
    assert key not in notifier.event_keys()
    mon.run_once()  # count 2/2 -> announce
    assert key in notifier.event_keys()
    ev = next(e for e in notifier.events if e.key == key)
    assert ev.tier == "activity"                       # opt-in, silent at default attention
    mon.run_once()  # still down -> edge-triggered, no re-announce
    assert notifier.event_keys().count(key) == 1
    assert fake.restarted == []                         # advisory NEVER restarts

    state["block"] = False
    mon.run_once()  # recovered -> resolve
    assert f"resolve:{key}" in notifier.event_keys()
