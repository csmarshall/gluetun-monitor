"""Per-URL timeout overrides vs the Docker transport read-timeout (#77).

Why: ``wget --spider`` emits nothing while it waits, so a probe that legitimately
runs past the transport ceiling is killed by docker-py and a slow *success* is
reported as a probe failure — the ``|timeout=`` override an operator adds to STOP
restarts would silently guarantee them. These tests pin the fix at each layer:
the transport is sized from the worst-case *effective* probe (startup + every
sites reload, raise-only), absurd overrides are rejected at parse time, and the
tunables doctor judges each site against its effective knobs so it never
re-suggests what's already set (or a lower value).
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.cli import _transport_timeout
from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStat
from gluetun_monitor.sites import (
    MAX_URL_TIMEOUT,
    SiteSpec,
    parse_entry,
    worst_case_probe_seconds,
)
from gluetun_monitor.tunables import suggest_tunables

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


# ----- worst_case_probe_seconds: the sizing input -----


def test_worst_case_is_global_product_without_overrides() -> None:
    specs = [SiteSpec("https://a.example"), SiteSpec("https://b.example")]
    assert worst_case_probe_seconds(specs, 10, 1) == 10
    assert worst_case_probe_seconds(specs, 10, 3) == 30


def test_worst_case_tracks_the_largest_effective_override() -> None:
    """timeout*tries is paired PER SITE: an override site uses its own knobs,
    filling any unset knob from the global."""
    specs = [
        SiteSpec("https://fast.example"),
        SiteSpec("https://slow.example", timeout=90),
        SiteSpec("https://retry.example", timeout=40, tries=3),  # 120 — the worst
    ]
    assert worst_case_probe_seconds(specs, 10, 1) == 120


# ----- parse_entry: absurd overrides are rejected, not transported -----


def test_parse_entry_rejects_timeout_above_cap() -> None:
    """An override beyond the sanity cap would stretch every loop and the
    transport with it — warn-and-skip per the forgiving+loud contract; the URL
    is still monitored on the global defaults."""
    spec, warnings = parse_entry(f"https://a.example|timeout={MAX_URL_TIMEOUT + 1}")
    assert spec is not None and spec.timeout is None
    assert any("exceeds the maximum" in w for w in warnings)


def test_parse_entry_accepts_timeout_at_cap() -> None:
    spec, warnings = parse_entry(f"https://a.example|timeout={MAX_URL_TIMEOUT}")
    assert spec is not None and spec.timeout == MAX_URL_TIMEOUT
    assert warnings == []


def test_parse_entry_rejects_tries_above_cap() -> None:
    spec, warnings = parse_entry("https://a.example|tries=6")
    assert spec is not None and spec.tries is None
    assert any("exceeds the maximum" in w for w in warnings)


# ----- the transport ceiling follows the overrides -----


def test_startup_transport_is_sized_from_the_effective_worst_case(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://fast.example\nhttps://slow.example|timeout=90\n")
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    assert _transport_timeout(cfg) == 180  # 90 * 1 try * 2


def test_startup_transport_keeps_the_pre77_shape_without_overrides(tmp_path: Path) -> None:
    """No overrides → max(TIMEOUT*2, 60): existing deployments size identically."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://a.example\n")
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    assert _transport_timeout(cfg) == 60


def test_startup_transport_survives_an_unreadable_sites_config(tmp_path: Path) -> None:
    cfg = Config(config_file=str(tmp_path / "missing.conf"), gluetun_container="gluetun")
    assert _transport_timeout(cfg) == 60  # falls back to the global-only sizing


def test_loop_raises_the_ceiling_on_sites_reload(tmp_path: Path) -> None:
    """The sites set reloads every loop; an override added at runtime must raise
    the transport ceiling before its wider probe first runs — a slow success
    within the override must never be misreported as a probe failure."""
    conf = tmp_path / "sites.conf"
    conf.write_text("https://slow.example|timeout=90\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = lambda name, cmd: ExecResult(0, "")  # every probe passes
    cfg = Config(config_file=str(conf), gluetun_container="gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None).run_once()
    assert 180 in fake.ensured_timeouts  # 90 * 1 try * 2


# ----- tunables judge each site against its EFFECTIVE knobs -----


# #25: suggestions are gated on recent evidence, so a fixture's site must look
# like it failed recently. Overridable per-test via _stat(last_failure=...).
_NOW = 1000.0


def _stat(**kw: object) -> SiteStat:
    st = SiteStat(first_seen=0.0)
    st.last_failure = _NOW  # fresh by default (#25 recency gate)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


def _slow_stat(max_latency_ms: int) -> SiteStat:
    # longest_fail_streak > 1: a slow site's read-timeouts cluster, so the stat
    # must not accidentally qualify for the isolated-blip retry suggestion.
    return _stat(
        total_polls=100, total_failures=5, failure_reasons={"timeout": 5},
        restarts_triggered=10, restarts_cleared=4, longest_fail_streak=3,
        recent_latencies=[1500, 1800, max_latency_ms],
    )


def test_no_resuggestion_at_or_below_the_existing_override() -> None:
    """Globals would say timeout=20; the site already carries timeout=30 — the
    doctor must judge against the effective knob and stay silent, not re-suggest
    a value at/below what's already set."""
    st = _slow_stat(13000)  # would suggest 20 against the 10s global
    specs = {"https://slow": SiteSpec("https://slow", timeout=30)}
    assert suggest_tunables(
        {"https://slow": st}, global_timeout=10, global_tries=1, now=_NOW, specs=specs
    ) == []


def test_overridden_site_still_gets_a_wider_suggestion_when_warranted() -> None:
    """Genuinely slower than its existing override → suggest above the EFFECTIVE
    value, and the paste-ready line carries the site's other override too."""
    st = _slow_stat(40000)  # suggest round_up_5(40+5) = 45 > effective 30
    specs = {"https://slow": SiteSpec("https://slow", timeout=30, tries=2)}
    (s,) = suggest_tunables(
        {"https://slow": st}, global_timeout=10, global_tries=1, now=_NOW, specs=specs
    )
    assert s.config_line == "https://slow|timeout=45|tries=2"


def test_timeout_suggestions_are_capped_at_the_parse_maximum() -> None:
    """Never emit a paste-ready line parse_entry would reject."""
    st = _slow_stat(400_000)  # would suggest 405s uncapped
    (s,) = suggest_tunables({"https://glacial": st}, global_timeout=10, global_tries=1, now=_NOW)
    assert s.config_line == f"https://glacial|timeout={MAX_URL_TIMEOUT}"


def test_tries_override_suppresses_the_retry_suggestion() -> None:
    """A site already carrying |tries=2 must not be told to set tries=2 just
    because the GLOBAL tries is 1."""
    st = _stat(
        total_polls=100, total_failures=3, failure_reasons={"connection": 3},
        restarts_triggered=2, restarts_cleared=2, longest_fail_streak=1,
        recent_latencies=[500],
    )
    specs = {"https://blippy": SiteSpec("https://blippy", tries=2)}
    assert suggest_tunables(
        {"https://blippy": st}, global_timeout=10, global_tries=1, now=_NOW, specs=specs
    ) == []


def test_tries_suggestion_preserves_an_existing_timeout_override() -> None:
    st = _stat(
        total_polls=100, total_failures=3, failure_reasons={"connection": 3},
        restarts_triggered=2, restarts_cleared=2, longest_fail_streak=1,
        recent_latencies=[500],
    )
    specs = {"https://blippy": SiteSpec("https://blippy", timeout=25)}
    (s,) = suggest_tunables(
        {"https://blippy": st}, global_timeout=10, global_tries=1, now=_NOW, specs=specs
    )
    assert s.config_line == "https://blippy|timeout=25|tries=2"
