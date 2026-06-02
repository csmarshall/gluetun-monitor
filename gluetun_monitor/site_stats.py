"""Persistent per-site statistics + a flaky-site advisory (ADR-0008).

Records, across monitor restarts, how each test site behaves over time so the
operator gets attribution ("which sites cause the restarts, how often") and a
historical "this site keeps being flaky" view — and so the monitor can advise
"<site> caused A of the last B restarts over the last <window>; review it."

This is **observability**, not control flow: it never changes whether the
monitor restarts. Persistence is best-effort — a missing/corrupt/unwritable
stats file degrades to in-memory and never blocks the monitor (Tenet 7). That's
why persisting it doesn't violate the stateless recovery stance (Tenets 8/9):
recovery stays in-memory and reset-on-restart; only the *stats* persist.

Note: every failing poll belongs to exactly one failure episode, so the average
episode length (in polls) is simply total_failures / failure_episodes.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__

Clock = Callable[[], float]


def categorize_failure(reason: str) -> str:
    """Bucket a wget failure ``reason`` into a coarse category for the breakdown."""
    low = reason.lower()
    if "bad address" in low or "resolve" in low or "name or service" in low or "dns" in low:
        return "dns"
    if "ssl" in low or "tls" in low or "certificate" in low:
        return "tls"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "refused" in low or "can't connect" in low or "cannot connect" in low or "connection" in low:
        return "connection"
    if "http" in low or "server returned" in low:
        return "http-error"
    return "other"


def _percentile(values: list[int], p: float) -> int:
    """Nearest-rank percentile (p in 0..100) of ``values``; 0 if empty."""
    if not values:
        return 0
    s = sorted(values)
    k = min(len(s) - 1, max(0, math.ceil(p / 100.0 * len(s)) - 1))
    return s[k]


@dataclass
class SiteStat:
    """Lifetime counters for one test site."""

    first_seen: float
    last_seen: float = 0.0  # last time this site was polled (for retention pruning)
    total_polls: int = 0
    total_failures: int = 0
    failure_episodes: int = 0  # count of distinct consecutive-failure runs
    longest_fail_streak: int = 0  # longest run of consecutive failed polls ever
    restarts_triggered: int = 0
    restarts_cleared: int = 0  # restarts after which this site recovered (effectiveness)
    current_fail_streak: int = 0
    last_failure: float | None = None
    last_success: float | None = None
    # Latency (ms) of recent *successful* polls — a bounded ring for percentiles.
    recent_latencies: list[int] = field(default_factory=list)
    # Failure counts bucketed by category (dns/tls/timeout/connection/http-error/other).
    failure_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def failure_rate(self) -> float:
        return self.total_failures / self.total_polls if self.total_polls else 0.0

    @property
    def avg_episode_polls(self) -> float:
        return self.total_failures / self.failure_episodes if self.failure_episodes else 0.0

    @property
    def restart_effectiveness(self) -> float | None:
        """Fraction of this site's restarts after which it recovered on re-verify,
        or None when the site has triggered no restarts (so callers render 'n/a'
        rather than a misleading 0% that reads as 'restarts never worked')."""
        if not self.restarts_triggered:
            return None
        return self.restarts_cleared / self.restarts_triggered

    def latency_summary(self) -> dict[str, int]:
        """min/avg/max + p50/p90/p99 (ms) over the recent successful-poll samples.

        p90 is the actionable tail; p99 is the robust extreme (unlike max, it
        ignores the top outliers); p50 is the median (vs the mean in `avg`).
        """
        lat = self.recent_latencies
        if not lat:
            return {"samples": 0, "avg": 0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p99": 0}
        return {
            "samples": len(lat),
            "avg": round(sum(lat) / len(lat)),
            "min": min(lat),
            "max": max(lat),
            "p50": _percentile(lat, 50),
            "p90": _percentile(lat, 90),
            "p99": _percentile(lat, 99),
        }


@dataclass
class MonitorStats:
    """Top-level (monitor-wide) counters, persisted across restarts."""

    version: str = ""
    first_started: float = 0.0  # first ever start (persisted once)
    last_started: float = 0.0  # this process's start
    total_loops: int = 0  # check cycles run (cumulative)
    total_runtime_seconds: float = 0.0  # accumulated running time (excludes downtime)
    total_gluetun_restarts: int = 0
    total_dependent_remediations: int = 0
    total_advisories: int = 0


@dataclass
class Advisory:
    """A flaky-site finding worth surfacing to the operator."""

    site: str
    site_restarts: int
    total_restarts: int
    window_seconds: int


class SiteStatsStore:
    """In-memory site stats with best-effort JSON persistence + advisory logic."""

    def __init__(
        self,
        path: str | None = None,
        *,
        clock: Clock = time.time,
        recent_restarts_max: int = 500,
        latency_ring_max: int = 200,
    ) -> None:
        self._path = Path(path) if path else None
        self._clock = clock
        self._recent_max = recent_restarts_max
        self._latency_max = latency_ring_max
        self.sites: dict[str, SiteStat] = {}
        self.recent_restarts: list[dict[str, object]] = []  # {"ts": float, "site": str}
        self.monitor = MonitorStats()
        self._load()
        # Stamp this process's start; first_started persists from the first ever run.
        now = self._clock()
        self.monitor.last_started = now
        self.monitor.first_started = self.monitor.first_started or now
        self.monitor.version = __version__
        self._last_tick = now  # for runtime accumulation

    def _stat(self, site: str) -> SiteStat:
        st = self.sites.get(site)
        if st is None:
            st = self.sites[site] = SiteStat(first_seen=self._clock())
        return st

    # ----- recording -----

    def record_poll(self, site: str, ok: bool, duration_ms: int = 0, reason: str = "") -> None:
        """Record one test of ``site`` (a primary loop poll; not the re-verify).

        On success the response time feeds the latency ring (for percentiles); on
        failure the reason is bucketed into the failure-reason breakdown.
        """
        now = self._clock()
        st = self._stat(site)
        st.last_seen = now
        st.total_polls += 1
        if ok:
            st.current_fail_streak = 0
            st.last_success = now
            st.recent_latencies.append(int(duration_ms))
            if len(st.recent_latencies) > self._latency_max:
                del st.recent_latencies[: -self._latency_max]
        else:
            st.total_failures += 1
            if st.current_fail_streak == 0:
                st.failure_episodes += 1  # a new failure episode begins
            st.current_fail_streak += 1
            st.longest_fail_streak = max(st.longest_fail_streak, st.current_fail_streak)
            st.last_failure = now
            cat = categorize_failure(reason)
            st.failure_reasons[cat] = st.failure_reasons.get(cat, 0) + 1

    def record_restart(self, site: str) -> None:
        """Record that ``site`` triggered a gluetun restart (attribution)."""
        now = self._clock()
        self._stat(site).restarts_triggered += 1
        self.recent_restarts.append({"ts": now, "site": site})
        if len(self.recent_restarts) > self._recent_max:
            del self.recent_restarts[: -self._recent_max]

    def record_loop(self) -> None:
        """Mark one check cycle: accumulate running time + bump the loop count.
        Downtime between runs isn't counted (the tick resets on restart)."""
        now = self._clock()
        self.monitor.total_runtime_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now
        self.monitor.total_loops += 1

    def record_gluetun_restart(self) -> None:
        """One gluetun restart initiated (monitor-wide count; counted when the
        action is taken, not gated on it clearing the failure)."""
        self.monitor.total_gluetun_restarts += 1

    def record_dependent_remediation(self) -> None:
        """One dependent remediation initiated — restart/recreate (monitor-wide
        count; counted when the action is taken, not gated on it succeeding)."""
        self.monitor.total_dependent_remediations += 1

    def record_advisory(self) -> None:
        """One flaky-site advisory raised (monitor-wide count)."""
        self.monitor.total_advisories += 1

    def record_restart_outcome(self, site: str, cleared: bool) -> None:
        """Record whether ``site`` recovered after the restart it triggered (the
        post-restart re-verify). Drives restart-effectiveness — a site whose
        restarts rarely clear it is the site's fault, not the VPN's."""
        if cleared:
            self._stat(site).restarts_cleared += 1

    def prune_stale(self, retention_seconds: float) -> list[str]:
        """Drop sites not polled within ``retention_seconds`` (e.g. removed from
        sites.conf 90+ days ago). Returns the pruned site names. A non-positive
        retention disables pruning (keep forever)."""
        if retention_seconds <= 0:
            return []
        cutoff = self._clock() - retention_seconds
        stale = [url for url, st in self.sites.items() if st.last_seen < cutoff]
        for url in stale:
            del self.sites[url]
        return stale

    # ----- advisory -----

    def advisory(
        self, window_seconds: int, min_restarts: int, dominance: float
    ) -> Advisory | None:
        """Return an Advisory if one site dominates recent restarts, else None.

        Fires when there have been >= ``min_restarts`` restarts within
        ``window_seconds`` and a single site accounts for >= ``dominance`` of them.
        """
        cutoff = self._clock() - window_seconds
        recent = [
            r for r in self.recent_restarts
            if isinstance(r.get("ts"), int | float) and float(r["ts"]) >= cutoff  # type: ignore[arg-type]
        ]
        if len(recent) < min_restarts:
            return None
        counts = Counter(str(r["site"]) for r in recent)
        site, top = counts.most_common(1)[0]
        if top / len(recent) >= dominance:
            return Advisory(site, top, len(recent), window_seconds)
        return None

    # ----- persistence (best-effort) -----

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for url, raw in data.get("sites", {}).items():
                first_seen = raw.get("first_seen", 0.0)
                self.sites[url] = SiteStat(
                    first_seen=first_seen,
                    # Old files predate last_seen; fall back so they aren't pruned
                    # as "stale" the moment they load.
                    last_seen=raw.get("last_seen") or first_seen,
                    total_polls=raw.get("total_polls", 0),
                    total_failures=raw.get("total_failures", 0),
                    failure_episodes=raw.get("failure_episodes", 0),
                    longest_fail_streak=raw.get("longest_fail_streak", 0),
                    restarts_triggered=raw.get("restarts_triggered", 0),
                    restarts_cleared=raw.get("restarts_cleared", 0),
                    current_fail_streak=raw.get("current_fail_streak", 0),
                    last_failure=raw.get("last_failure"),
                    last_success=raw.get("last_success"),
                    recent_latencies=[int(x) for x in raw.get("recent_latencies", [])
                                      if isinstance(x, int | float)],
                    failure_reasons={str(k): int(v) for k, v in
                                     (raw.get("failure_reasons") or {}).items()},
                )
            recent = data.get("recent_restarts", [])
            self.recent_restarts = [
                r for r in recent if isinstance(r, dict) and "ts" in r and "site" in r
            ]
            m = data.get("monitor") or {}
            # `version` and `last_started` are intentionally NOT reloaded: version
            # is re-stamped from the running build (the persisted one is for human
            # reading only), and last_started is this process's start, set live.
            self.monitor = MonitorStats(
                first_started=m.get("first_started", 0.0),
                total_loops=m.get("total_loops", 0),
                total_runtime_seconds=m.get("total_runtime_seconds", 0.0),
                total_gluetun_restarts=m.get("total_gluetun_restarts", 0),
                total_dependent_remediations=m.get("total_dependent_remediations", 0),
                total_advisories=m.get("total_advisories", 0),
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # Corrupt/unreadable stats are non-fatal — start fresh in memory. This
            # must tolerate *any* shape of garbage, not just truncation: a file
            # whose top level or a "sites"/"monitor" value is the wrong type (e.g.
            # a JSON list) would otherwise raise AttributeError on .get()/.items()
            # and crash startup, defeating the best-effort contract (Tenet 1).
            self.sites = {}
            self.recent_restarts = []
            self.monitor = MonitorStats()

    def save(self) -> bool:
        """Write stats to disk crash- and power-loss-safely. Returns False (and is
        ignored) on any I/O error — stats must never block the monitor.

        Pattern: serialize to a temp file, ``fsync`` it, then atomically rename
        over the target (``rename(2)``), then fsync the directory. A process crash
        or container restart can only leave the *old* complete file or the *new*
        complete file — never a partial/corrupt one — and the reader tolerates a
        bad file regardless (``_load`` falls back to empty). The fsyncs extend that
        guarantee across a host power loss, not just a process crash.
        """
        if self._path is None:
            return False
        # Persist raw counters (asdict) + computed metrics, so the file is both
        # reloadable and human-readable at a glance.
        sites_out = {}
        for url, st in self.sites.items():
            raw = asdict(st)
            raw["failure_rate"] = round(st.failure_rate, 4)
            raw["avg_episode_polls"] = round(st.avg_episode_polls, 2)
            eff = st.restart_effectiveness
            raw["restart_effectiveness"] = round(eff, 4) if eff is not None else None
            raw["latency_ms"] = st.latency_summary()
            sites_out[url] = raw
        now = self._clock()
        monitor_out = asdict(self.monitor)
        monitor_out["current_uptime_seconds"] = round(max(0.0, now - self.monitor.last_started))
        payload = {
            "version": 1,
            "updated": now,
            "monitor": monitor_out,
            "sites": sites_out,
            "recent_restarts": self.recent_restarts,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())  # data is on disk before the rename
            tmp.replace(self._path)  # atomic rename
            self._fsync_dir(self._path.parent)  # persist the rename itself
            return True
        except OSError:
            return False

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort fsync of a directory so a rename survives power loss."""
        try:
            fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def format_window(seconds: int) -> str:
    """Human-readable window like '24h' / '90m' / '7d' for advisory messages."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
