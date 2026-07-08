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

# #25: suggestions are gated on recent evidence, so a fixture's site must look
# like it failed recently. Overridable per-test via _stat(last_failure=...).
_NOW = 1000.0


def _stat(**kw: object) -> SiteStat:
    st = SiteStat(first_seen=0.0)
    st.last_failure = _NOW  # fresh by default (#25 recency gate)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


def _suggest(sites: dict[str, SiteStat], *, timeout: int = 10, tries: int = 1):
    return suggest_tunables(sites, global_timeout=timeout, global_tries=tries, now=_NOW)


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


def test_advisory_site_gets_no_suggestion() -> None:
    """#110 review: an advisory site never restarts, so 'widen its timeout to avoid
    restarts' is false advice — skip it. Same stat still suggests when critical."""
    from gluetun_monitor.sites import SiteSpec

    url = "https://slow"
    st = _stat(total_polls=100, total_failures=5, failure_reasons={"timeout": 5},
               restarts_triggered=10, restarts_cleared=4, recent_latencies=[1500, 1800, 13000])
    assert suggest_tunables({url: st}, global_timeout=10, global_tries=1, now=_NOW,
                            specs={url: SiteSpec(url, role="critical")})           # critical -> suggests
    assert suggest_tunables({url: st}, global_timeout=10, global_tries=1, now=_NOW,
                            specs={url: SiteSpec(url, role="advisory")}) == []      # advisory -> skipped


# ----- #25: suggestions are gated on recent evidence -----


def _damning() -> SiteStat:
    """Lifetime counters that qualify for a timeout bump: a pattern of read-timeouts
    plus a success near the ceiling. Recency is what the tests below vary."""
    return _stat(
        total_polls=100, total_failures=5, failure_reasons={"timeout": 5},
        restarts_triggered=10, restarts_cleared=4, longest_fail_streak=3,
        recent_latencies=[1500, 1800, 13000],
    )


def test_stale_site_yields_no_suggestion_however_damning_its_lifetime_counters() -> None:
    """The core #25 regression: every gate reads a lifetime-monotone counter, so a site
    that misbehaved a month ago and has been healthy since used to qualify forever."""
    st = _damning()
    st.last_failure = _NOW - 90_000  # 25h ago, outside the 24h window
    assert suggest_tunables({"https://slow": st}, global_timeout=10, global_tries=1,
                            now=_NOW, stale_after_seconds=86_400) == []


def test_recent_failure_still_yields_the_suggestion() -> None:
    """The gate suppresses stale advice, not useful advice."""
    st = _damning()
    st.last_failure = _NOW - 3_600  # an hour ago — still a live problem
    (s,) = suggest_tunables({"https://slow": st}, global_timeout=10, global_tries=1,
                            now=_NOW, stale_after_seconds=86_400)
    assert s.kind == "timeout"


def test_never_failed_site_yields_no_suggestion() -> None:
    """`last_failure is None` — nothing has ever gone wrong, so there is nothing to tune
    (its failure counters are empty anyway; the gate just says so explicitly)."""
    st = _damning()
    st.last_failure = None
    assert suggest_tunables({"https://slow": st}, global_timeout=10, global_tries=1,
                            now=_NOW, stale_after_seconds=86_400) == []


def test_recency_window_boundary_is_inclusive() -> None:
    """A failure exactly at the window edge still counts; one second older does not."""
    at_edge = _damning()
    at_edge.last_failure = _NOW - 86_400
    past_edge = _damning()
    past_edge.last_failure = _NOW - 86_401
    assert suggest_tunables({"https://slow": at_edge}, global_timeout=10, global_tries=1,
                            now=_NOW, stale_after_seconds=86_400)
    assert suggest_tunables({"https://slow": past_edge}, global_timeout=10, global_tries=1,
                            now=_NOW, stale_after_seconds=86_400) == []
