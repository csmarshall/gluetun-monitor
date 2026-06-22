"""The data-driven per-URL tunable suggestion engine (#60).

Why: the suggestion is what turns the persisted stats into action — it must (a)
tell a slow-but-alive site (widen its timeout) from a genuinely-dead one (a longer
timeout won't help → review/#27), (b) not fire on one-off blips, and (c) rank the
biggest restart driver first. Pure function over SiteStat, so it's pinned here
against hand-built fixtures rather than a live run.
"""

from __future__ import annotations

from gluetun_monitor.site_stats import SiteStat
from gluetun_monitor.tunables import suggest_tunables


def _stat(**kw: object) -> SiteStat:
    st = SiteStat(first_seen=0.0)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


def _suggest(sites: dict[str, SiteStat], *, timeout: int = 10, tries: int = 1):
    return suggest_tunables(sites, global_timeout=timeout, global_tries=tries)


def test_slow_but_alive_gets_a_timeout_bump() -> None:
    """A pattern of read-timeouts plus successful responses near/over the ceiling
    → widen the timeout, anchored above the slowest observed success."""
    st = _stat(
        total_polls=100, total_failures=5, failure_reasons={"timeout": 5},
        restarts_triggered=10, restarts_cleared=4,
        recent_latencies=[1500, 1800, 13000],  # max well over the 10s ceiling
    )
    (s,) = _suggest({"https://slow": st})
    assert s.kind == "timeout"
    assert s.config_line == "https://slow|timeout=20"  # round_up_5(ceil(13)+5)
    assert "slow, not dead" in s.rationale


def test_single_timeout_blip_is_not_a_pattern() -> None:
    """One read-timeout on an otherwise-fast site is noise — no per-URL override
    (it would only delay detecting a real outage)."""
    st = _stat(
        total_polls=100, total_failures=1, failure_reasons={"timeout": 1},
        restarts_triggered=0, recent_latencies=[600, 11000],
    )
    assert _suggest({"https://fast": st}) == []


def test_ineffective_dns_failures_point_at_removal_or_backoff() -> None:
    """Restarts that rarely clear it + DNS/connection failures (not slowness) →
    'investigate' advice, no paste-able knob (a timeout won't help)."""
    st = _stat(
        total_polls=200, total_failures=25,
        failure_reasons={"dns": 20, "connection": 5},
        restarts_triggered=10, restarts_cleared=0,  # effectiveness 0.0
        recent_latencies=[800, 900],
    )
    (s,) = _suggest({"https://dead": st})
    assert s.kind == "investigate"
    assert s.config_line is None
    assert "#27" in s.rationale


def test_isolated_blips_suggest_a_retry() -> None:
    """Single-poll failures (never chaining) that still trip a restart → tries=2
    absorbs the transient before it counts."""
    st = _stat(
        total_polls=100, total_failures=3, failure_reasons={"connection": 3},
        restarts_triggered=3, restarts_cleared=3, longest_fail_streak=1,
        recent_latencies=[700, 800],
    )
    (s,) = _suggest({"https://blippy": st})
    assert s.kind == "tries"
    assert s.config_line == "https://blippy|tries=2"


def test_tries_not_suggested_when_already_retrying() -> None:
    """If WGET_TRIES is already >1, the retry suggestion is moot — stay silent."""
    st = _stat(
        total_polls=100, total_failures=3, failure_reasons={"connection": 3},
        restarts_triggered=3, restarts_cleared=3, longest_fail_streak=1,
        recent_latencies=[700],
    )
    assert _suggest({"https://blippy": st}, tries=2) == []


def test_healthy_site_yields_nothing() -> None:
    st = _stat(total_polls=10000, total_failures=2, recent_latencies=[600, 700])
    assert _suggest({"https://good": st}) == []


def test_suggestions_ranked_by_restarts() -> None:
    """Most-impactful first so the operator fixes the biggest restart driver."""
    big = _stat(
        total_polls=100, total_failures=20, failure_reasons={"timeout": 20},
        restarts_triggered=50, restarts_cleared=20, recent_latencies=[13000],
    )
    small = _stat(
        total_polls=100, total_failures=4, failure_reasons={"timeout": 4},
        restarts_triggered=5, restarts_cleared=2, recent_latencies=[12000],
    )
    out = _suggest({"https://small": small, "https://big": big})
    assert [s.url for s in out] == ["https://big", "https://small"]
