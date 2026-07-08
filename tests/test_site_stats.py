"""Persistent per-site stats + flaky-site advisory (ADR-0008).

Why: this is the lifetime/attribution data behind "this site keeps being flaky"
and the advisory. The episode accounting (avg failure length), the windowed
advisory (one site dominating recent restarts), and best-effort persistence
(survives restart; corrupt file never crashes) are the load-bearing behaviors.
"""

from __future__ import annotations

from pathlib import Path

from gluetun_monitor.site_stats import SiteStatsStore, categorize_failure, format_window


class _Clock:
    """Mutable fake clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_episode_accounting_and_derived_metrics() -> None:
    """fail,fail,pass,fail = 3 failures across 2 episodes (avg 1.5 polls); a pass
    closes an episode so the next failure starts a new one."""
    s = SiteStatsStore(None)
    for ok in (False, False, True, False):
        s.record_poll("b.com", ok)
    st = s.sites["b.com"]
    assert st.total_polls == 4
    assert st.total_failures == 3
    assert st.failure_episodes == 2
    assert st.avg_episode_polls == 1.5
    assert st.failure_rate == 0.75


def test_longest_fail_streak_tracks_the_worst_run() -> None:
    """longest_fail_streak = the longest run of consecutive failed polls ever,
    even after a recovery shortens the current streak."""
    s = SiteStatsStore(None)
    for ok in (False, False, False, True, False):  # runs of 3 then 1
        s.record_poll("b.com", ok)
    st = s.sites["b.com"]
    assert st.longest_fail_streak == 3
    assert st.current_fail_streak == 1


def test_last_seen_updates_on_poll() -> None:
    clock = _Clock(500.0)
    s = SiteStatsStore(None, clock=clock)
    s.record_poll("b.com", True)
    assert s.sites["b.com"].last_seen == 500.0
    clock.t = 900.0
    s.record_poll("b.com", True)
    assert s.sites["b.com"].last_seen == 900.0


def test_prune_stale_drops_unseen_sites_keeps_fresh() -> None:
    """A site not polled within retention (e.g. removed from sites.conf) is pruned;
    a recently-polled one is kept."""
    clock = _Clock(1_000_000.0)
    s = SiteStatsStore(None, clock=clock)
    s.record_poll("old.com", True)
    s.record_poll("fresh.com", True)
    clock.t += 100 * 86400  # 100 days later
    s.record_poll("fresh.com", True)  # fresh.com seen again; old.com not
    pruned = s.prune_stale(90 * 86400)
    assert pruned == ["old.com"]
    assert "old.com" not in s.sites
    assert "fresh.com" in s.sites


def test_prune_disabled_with_nonpositive_retention() -> None:
    clock = _Clock(1_000_000.0)
    s = SiteStatsStore(None, clock=clock)
    s.record_poll("old.com", True)
    clock.t += 1000 * 86400
    assert s.prune_stale(0) == []  # 0 disables pruning
    assert "old.com" in s.sites


def test_save_is_atomic_no_leftover_tmp(tmp_path: Path) -> None:
    """A successful save leaves the real file complete and no stray .tmp behind."""
    path = tmp_path / "stats.json"
    s = SiteStatsStore(str(path))
    s.record_poll("b.com", False)
    assert s.save() is True
    assert path.exists()
    assert not (tmp_path / "stats.json.tmp").exists()  # tmp renamed away
    import json as _json
    data = _json.loads(path.read_text())  # the file is complete, valid JSON
    assert data["sites"]["b.com"]["total_failures"] == 1


def test_advisory_fires_when_one_site_dominates() -> None:
    """>= min_restarts in window, one site >= dominance share -> Advisory."""
    s = SiteStatsStore(None)
    for _ in range(4):
        s.record_restart("b.com")
    for _ in range(2):
        s.record_restart("a.com")
    adv = s.advisory(window_seconds=86400, min_restarts=5, dominance=0.5)
    assert adv is not None
    assert adv.site == "b.com"
    assert adv.site_restarts == 4
    assert adv.total_restarts == 6


def test_advisory_silent_below_min_restarts() -> None:
    s = SiteStatsStore(None)
    for _ in range(3):
        s.record_restart("b.com")
    assert s.advisory(86400, 5, 0.5) is None


def test_advisory_silent_when_no_site_dominates() -> None:
    s = SiteStatsStore(None)
    for _ in range(3):
        s.record_restart("a.com")
    for _ in range(3):
        s.record_restart("b.com")
    # 3/6 = 0.5 each; require >0.5 dominance -> nobody dominates.
    assert s.advisory(86400, 5, 0.6) is None


def test_advisory_respects_the_window() -> None:
    """Restarts older than the window don't count."""
    clock = _Clock(1000.0)
    s = SiteStatsStore(None, clock=clock)
    for _ in range(6):
        s.record_restart("b.com")
    clock.t = 1000.0 + 86400 + 1  # advance past the window
    assert s.advisory(86400, 5, 0.5) is None


def test_persistence_round_trip(tmp_path: Path) -> None:
    """Stats survive a 'restart' — written by one store, loaded by the next."""
    path = str(tmp_path / "stats.json")
    s1 = SiteStatsStore(path)
    s1.record_poll("b.com", False)
    s1.record_poll("b.com", True)
    s1.record_restart("b.com")
    assert s1.save() is True

    s2 = SiteStatsStore(path)
    assert s2.sites["b.com"].total_polls == 2
    assert s2.sites["b.com"].total_failures == 1
    assert s2.sites["b.com"].restarts_triggered == 1
    assert len(s2.recent_restarts) == 1


def test_corrupt_file_is_non_fatal(tmp_path: Path) -> None:
    """A garbage stats file degrades to empty in-memory, never raises."""
    path = tmp_path / "stats.json"
    path.write_text("{not valid json", encoding="utf-8")
    s = SiteStatsStore(str(path))
    assert s.sites == {}
    assert s.recent_restarts == []


def test_save_with_no_path_is_noop() -> None:
    assert SiteStatsStore(None).save() is False


def test_recent_restarts_bounded() -> None:
    s = SiteStatsStore(None, recent_restarts_max=10)
    for _ in range(25):
        s.record_restart("b.com")
    assert len(s.recent_restarts) == 10
    assert s.sites["b.com"].restarts_triggered == 25  # lifetime count is not capped


def test_latency_summary_avg_min_max_and_percentiles() -> None:
    """Successful-poll durations feed avg/min/max + p50/p75/p99 (median vs mean)."""
    s = SiteStatsStore(None)
    # 1..100 ms on passing polls (failures don't contribute latency).
    for ms in range(1, 101):
        s.record_poll("b.com", True, duration_ms=ms)
    lat = s.sites["b.com"].latency_summary()
    assert lat["samples"] == 100
    assert lat["min"] == 1 and lat["max"] == 100
    assert lat["avg"] == 50  # round(50.5) -> 50
    assert lat["p50"] == 50
    assert lat["p90"] == 90
    assert lat["p99"] == 99


def test_latency_ring_is_bounded() -> None:
    s = SiteStatsStore(None, latency_ring_max=50)
    for ms in range(1, 201):
        s.record_poll("b.com", True, duration_ms=ms)
    lat = s.sites["b.com"]
    assert len(lat.recent_latencies) == 50
    assert lat.recent_latencies[-1] == 200  # newest retained


def test_failed_polls_do_not_add_latency() -> None:
    s = SiteStatsStore(None)
    s.record_poll("b.com", False, duration_ms=9999, reason="timed out")
    assert s.sites["b.com"].recent_latencies == []


def test_failure_reason_breakdown() -> None:
    """Failures are bucketed by category from the reason text."""
    s = SiteStatsStore(None)
    s.record_poll("b.com", False, reason="Read error (Operation timed out)")
    s.record_poll("b.com", False, reason="Unable to establish SSL connection")
    s.record_poll("b.com", False, reason="wget: bad address 'b.com'")
    s.record_poll("b.com", False, reason="can't connect: Connection refused")
    fr = s.sites["b.com"].failure_reasons
    assert fr == {"timeout": 1, "tls": 1, "dns": 1, "connection": 1}


def test_categorize_failure_buckets() -> None:
    assert categorize_failure("Read error (Operation timed out)") == "timeout"
    assert categorize_failure("Unable to establish SSL connection") == "tls"
    assert categorize_failure("wget: bad address 'x'") == "dns"
    assert categorize_failure("Connection refused") == "connection"
    assert categorize_failure("server returned error: HTTP/1.1 500") == "http-error"
    assert categorize_failure("something weird") == "other"


def test_restart_effectiveness() -> None:
    """Of a site's restarts, the fraction that cleared it on re-verify."""
    s = SiteStatsStore(None)
    for _ in range(4):
        s.record_restart("b.com")
    s.record_restart_outcome("b.com", cleared=True)
    s.record_restart_outcome("b.com", cleared=True)
    s.record_restart_outcome("b.com", cleared=False)
    s.record_restart_outcome("b.com", cleared=False)
    st = s.sites["b.com"]
    assert st.restarts_triggered == 4
    assert st.restarts_cleared == 2
    assert st.restart_effectiveness == 0.5


def test_restart_effectiveness_none_when_no_restarts() -> None:
    """No restarts triggered -> None (callers render 'n/a', not a misleading 0%)."""
    s = SiteStatsStore(None)
    s.record_poll("b.com", True, duration_ms=10)
    assert s.sites["b.com"].restart_effectiveness is None


def test_lifetime_histogram_fed_and_persists(tmp_path: Path) -> None:
    """Successful polls feed the all-time histogram, which survives a save/reload."""
    path = str(tmp_path / "stats.json")
    s1 = SiteStatsStore(path)
    for ms in (100, 200, 300, 400, 500):
        s1.record_poll("b.com", True, duration_ms=ms)
    hist1 = s1.sites["b.com"].lifetime_latency
    assert hist1.count == 5 and hist1.summary()["avg"] == 300
    assert s1.save() is True

    s2 = SiteStatsStore(path)
    hist2 = s2.sites["b.com"].lifetime_latency
    assert hist2.count == 5
    assert hist2.summary() == hist1.summary()  # percentiles + exacts survive reload

    # And the human-readable lifetime summary is in the JSON.
    import json as _json
    data = _json.loads((tmp_path / "stats.json").read_text())
    assert "lifetime_latency_ms" in data["sites"]["b.com"]
    assert data["sites"]["b.com"]["lifetime_latency_ms"]["samples"] == 5


def test_lifetime_histogram_outlives_recent_ring(tmp_path: Path) -> None:
    """The ring is bounded (recent view); the histogram keeps all-time count."""
    s = SiteStatsStore(None, latency_ring_max=10)
    for ms in range(1, 101):  # 100 successful polls
        s.record_poll("b.com", True, duration_ms=ms * 10)
    st = s.sites["b.com"]
    assert len(st.recent_latencies) == 10  # ring capped
    assert st.lifetime_latency.count == 100  # histogram kept them all


def test_new_metrics_persist_round_trip(tmp_path: Path) -> None:
    path = str(tmp_path / "stats.json")
    s1 = SiteStatsStore(path)
    s1.record_poll("b.com", True, duration_ms=42)
    s1.record_poll("b.com", False, reason="timed out")
    s1.record_restart("b.com")
    s1.record_restart_outcome("b.com", cleared=True)
    assert s1.save() is True

    s2 = SiteStatsStore(path)
    st = s2.sites["b.com"]
    assert st.recent_latencies == [42]
    assert st.failure_reasons == {"timeout": 1}
    assert st.restarts_cleared == 1

    # The saved JSON also carries human-readable computed metrics.
    import json as _json
    data = _json.loads((tmp_path / "stats.json").read_text())
    site = data["sites"]["b.com"]
    assert "latency_ms" in site and site["latency_ms"]["avg"] == 42
    assert "restart_effectiveness" in site and "failure_rate" in site


def test_monitor_level_counters() -> None:
    """Top-level (monitor-wide) counters: loops, restarts, remediations, advisories."""
    s = SiteStatsStore(None)
    for _ in range(3):
        s.record_loop()
    s.record_gluetun_restart()
    s.record_gluetun_restart()
    s.record_dependent_remediation("dep")
    s.record_advisory()
    assert s.monitor.total_loops == 3
    assert s.monitor.total_gluetun_restarts == 2
    assert s.monitor.total_dependent_remediations == 1
    assert s.monitor.total_advisories == 1
    assert s.monitor.version  # stamped from the package version


def test_monitor_runtime_accumulates_excluding_downtime() -> None:
    """Runtime accrues between loops; the gap before the first loop of a process
    doesn't count as runtime (the tick is set at start)."""
    clock = _Clock(1000.0)
    s = SiteStatsStore(None, clock=clock)
    clock.t = 1005.0
    s.record_loop()  # +5s
    clock.t = 1008.0
    s.record_loop()  # +3s
    assert s.monitor.total_runtime_seconds == 8.0


def test_monitor_stats_persist_and_first_started_is_stable(tmp_path: Path) -> None:
    """Cumulative monitor counters survive a restart; first_started is set once,
    last_started refreshes each process."""
    path = str(tmp_path / "stats.json")
    clock = _Clock(1000.0)
    s1 = SiteStatsStore(path, clock=clock)
    s1.record_loop()
    s1.record_gluetun_restart()
    assert s1.save() is True

    clock.t = 5000.0  # "restart" later
    s2 = SiteStatsStore(path, clock=clock)
    assert s2.monitor.total_loops == 1  # carried over
    assert s2.monitor.total_gluetun_restarts == 1
    assert s2.monitor.first_started == 1000.0  # set once, preserved
    assert s2.monitor.last_started == 5000.0  # this process

    import json as _json
    data = _json.loads((tmp_path / "stats.json").read_text())
    assert "monitor" in data
    s2.save()
    data = _json.loads((tmp_path / "stats.json").read_text())
    assert "current_uptime_seconds" in data["monitor"]


def test_format_window() -> None:
    assert format_window(86400) == "1d"
    assert format_window(3600) == "1h"
    assert format_window(1800) == "30m"
    assert format_window(90) == "90s"


def test_advisory_empty_window_is_none_even_if_min_restarts_zero() -> None:
    """advisory() must not IndexError on an empty most_common() when min_restarts<=0
    (review finding — the public method now guards `not recent`)."""
    store = SiteStatsStore(None)
    assert store.advisory(3600, 0, 0.5) is None


# ----- #45 dependent-flapping advisory -----


def test_dependent_advisories_counts_per_dependent() -> None:
    """Count-based (no dominance): each dependent flagged independently once it hits
    the threshold; a dependent below it isn't."""
    clock = _Clock(1000.0)
    s = SiteStatsStore(None, clock=clock)
    for _ in range(3):
        s.record_dependent_remediation("flappy")
    for _ in range(2):
        s.record_dependent_remediation("occasional")
    advs = {a.dependent: a.remediations for a in s.dependent_advisories(100, 3)}
    assert advs == {"flappy": 3}  # occasional (2) is below the min


def test_dependent_advisories_respects_window() -> None:
    clock = _Clock(1000.0)
    s = SiteStatsStore(None, clock=clock)
    for _ in range(5):
        s.record_dependent_remediation("dep")
    assert len(s.dependent_advisories(100, 5)) == 1
    clock.t += 200  # all five remediations now fall outside the 100s window
    assert s.dependent_advisories(100, 5) == []


def test_recent_remediations_survive_persistence(tmp_path: Path) -> None:
    clock = _Clock(1000.0)
    p = tmp_path / "stats.json"
    s = SiteStatsStore(str(p), clock=clock)
    for _ in range(4):
        s.record_dependent_remediation("dep")
    assert s.save()
    reloaded = SiteStatsStore(str(p), clock=clock)
    assert len(reloaded.dependent_advisories(100, 4)) == 1  # counter survived a restart
