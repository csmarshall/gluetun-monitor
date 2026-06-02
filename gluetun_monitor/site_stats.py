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
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

Clock = Callable[[], float]


@dataclass
class SiteStat:
    """Lifetime counters for one test site."""

    first_seen: float
    total_polls: int = 0
    total_failures: int = 0
    failure_episodes: int = 0  # count of distinct consecutive-failure runs
    restarts_triggered: int = 0
    current_fail_streak: int = 0
    last_failure: float | None = None
    last_success: float | None = None

    @property
    def failure_rate(self) -> float:
        return self.total_failures / self.total_polls if self.total_polls else 0.0

    @property
    def avg_episode_polls(self) -> float:
        return self.total_failures / self.failure_episodes if self.failure_episodes else 0.0


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
    ) -> None:
        self._path = Path(path) if path else None
        self._clock = clock
        self._recent_max = recent_restarts_max
        self.sites: dict[str, SiteStat] = {}
        self.recent_restarts: list[dict[str, object]] = []  # {"ts": float, "site": str}
        self._load()

    # ----- recording -----

    def record_poll(self, site: str, ok: bool) -> None:
        """Record one test of ``site`` (a primary loop poll; not the re-verify)."""
        now = self._clock()
        st = self.sites.get(site)
        if st is None:
            st = self.sites[site] = SiteStat(first_seen=now)
        st.total_polls += 1
        if ok:
            st.current_fail_streak = 0
            st.last_success = now
        else:
            st.total_failures += 1
            if st.current_fail_streak == 0:
                st.failure_episodes += 1  # a new failure episode begins
            st.current_fail_streak += 1
            st.last_failure = now

    def record_restart(self, site: str) -> None:
        """Record that ``site`` triggered a gluetun restart (attribution)."""
        now = self._clock()
        st = self.sites.get(site)
        if st is None:
            st = self.sites[site] = SiteStat(first_seen=now)
        st.restarts_triggered += 1
        self.recent_restarts.append({"ts": now, "site": site})
        if len(self.recent_restarts) > self._recent_max:
            del self.recent_restarts[: -self._recent_max]

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
                self.sites[url] = SiteStat(
                    first_seen=raw.get("first_seen", 0.0),
                    total_polls=raw.get("total_polls", 0),
                    total_failures=raw.get("total_failures", 0),
                    failure_episodes=raw.get("failure_episodes", 0),
                    restarts_triggered=raw.get("restarts_triggered", 0),
                    current_fail_streak=raw.get("current_fail_streak", 0),
                    last_failure=raw.get("last_failure"),
                    last_success=raw.get("last_success"),
                )
            recent = data.get("recent_restarts", [])
            self.recent_restarts = [
                r for r in recent if isinstance(r, dict) and "ts" in r and "site" in r
            ]
        except (OSError, ValueError, TypeError):
            # Corrupt/unreadable stats are non-fatal — start fresh in memory.
            self.sites = {}
            self.recent_restarts = []

    def save(self) -> bool:
        """Write stats to disk atomically. Returns False (and is ignored) on any
        I/O error — stats must never block the monitor."""
        if self._path is None:
            return False
        payload = {
            "version": 1,
            "updated": self._clock(),
            "sites": {url: asdict(st) for url, st in self.sites.items()},
            "recent_restarts": self.recent_restarts,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)  # atomic
            return True
        except OSError:
            return False


def format_window(seconds: int) -> str:
    """Human-readable window like '24h' / '90m' / '7d' for advisory messages."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
