"""Data-driven per-URL tunable suggestions (#60).

A pure analysis over the persisted per-site stats (ADR-0008): given how each site
has actually behaved, recommend the per-URL ``|timeout=`` / ``|tries=`` override
that would stop it causing needless gluetun restarts — or, when no knob can help
(it's genuinely unreachable), point the operator at removal / the #27 backoff.

This is advisory only and never auto-applies (Tenet 1). It leans entirely on data
already in ``site-stats.json``, so it's a side report the operator runs on demand
(``--suggest-tunables``) — no new collection, no log noise, and trivially testable
against a stats fixture.

The discriminator is the same one the live data validated: a site that *times out*
yet has *answered* at latencies near/above the current ceiling is **slow, not
dead** — widen its timeout and keep it a useful canary. A site whose restarts
rarely clear it (low ``restart_effectiveness``) and whose failures are DNS/connect
(not slow reads) is the site's fault, not the VPN's — a longer timeout won't help.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .site_stats import SiteStat

# A site counts as "slow, not dead" when it has answered at >= this fraction of the
# current timeout ceiling — proof a wider timeout would convert its read-timeouts
# into passes (rather than the failure being a hard hang a longer wait won't fix).
_SLOW_LATENCY_FRACTION = 0.7
# Minimum read-timeout failures before suggesting a wider timeout — a pattern, not
# a one-off blip (a single slow read on an otherwise-fast site isn't worth pinning
# a higher per-URL timeout that would only delay detecting a real outage).
_MIN_TIMEOUT_FAILURES = 3
# Headroom (seconds) added above the slowest observed *successful* response when
# suggesting a new timeout — enough that the occasional slow read still lands.
_TIMEOUT_HEADROOM_S = 5
# Minimum restarts before the "restarts don't help — investigate" advice fires;
# below this it's noise, not a pattern.
_INVESTIGATE_MIN_RESTARTS = 3


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One per-URL recommendation derived from a site's history.

    ``config_line`` is the paste-ready ``url|key=value`` for the ``timeout``/
    ``tries`` kinds, or ``None`` for ``investigate`` (there's nothing to paste —
    the advice is to review/remove the site or enable the #27 backoff).
    """

    url: str
    kind: str  # "timeout" | "tries" | "investigate"
    config_line: str | None
    rationale: str
    restarts: int


def _round_up_5(n: int) -> int:
    """Round ``n`` up to the nearest 5 — tidy, human-friendly timeout values."""
    return ((n + 4) // 5) * 5


def _latency(st: SiteStat) -> dict[str, int]:
    """Best available latency summary: lifetime histogram if it has samples, else
    the recent ring (a freshly-seen site may only have the ring populated).
    """
    lifetime = st.lifetime_latency.summary()
    return lifetime if lifetime.get("samples", 0) else st.latency_summary()


def _suggest_one(
    url: str, st: SiteStat, *, global_timeout: int, global_tries: int
) -> Suggestion | None:
    """The single best suggestion for one site, or None if it's behaving fine.

    Priority is by *which fix actually helps*: a wider timeout for a slow-but-alive
    site (the surgical keep-it-useful fix), else "investigate" when restarts are
    ineffective and the cause isn't slowness, else a retry bump to absorb isolated
    one-poll blips that tripped a restart.
    """
    reasons = st.failure_reasons
    timeouts = reasons.get("timeout", 0)
    unreachable = reasons.get("dns", 0) + reasons.get("connection", 0)
    lat = _latency(st)
    max_ms, p99 = lat.get("max", 0), lat.get("p99", 0)
    eff = st.restart_effectiveness

    # (1) Slow-but-alive: a *pattern* of read-timeouts AND it has answered
    # near/over the ceiling (so a wider timeout converts those into passes).
    if timeouts >= _MIN_TIMEOUT_FAILURES and max_ms >= global_timeout * 1000 * _SLOW_LATENCY_FRACTION:
        suggested = _round_up_5(math.ceil(max_ms / 1000) + _TIMEOUT_HEADROOM_S)
        if suggested > global_timeout:
            return Suggestion(
                url, "timeout", f"{url}|timeout={suggested}",
                f"{timeouts} read-timeout failure(s) but answers within "
                f"p99={p99}ms / max={max_ms}ms — slow, not dead; a {suggested}s timeout "
                f"keeps it a useful canary instead of triggering restarts",
                st.restarts_triggered,
            )

    # (2) Restarts don't help and it isn't slowness — review/remove or #27 backoff.
    if (
        eff is not None and eff < 0.5
        and st.restarts_triggered >= _INVESTIGATE_MIN_RESTARTS
        and unreachable >= timeouts
    ):
        return Suggestion(
            url, "investigate", None,
            f"triggered {st.restarts_triggered} restart(s) that cleared it only "
            f"{eff:.0%} of the time, mostly DNS/connection failures — a longer timeout "
            f"won't help; review/remove it or enable auto-backoff (#27)",
            st.restarts_triggered,
        )

    # (3) Isolated one-poll blips that nonetheless tripped a restart — a retry
    # absorbs the transient before it counts (only when not already retrying).
    if (
        global_tries < 2
        and st.restarts_triggered > 0
        and st.total_failures > 0
        and st.longest_fail_streak <= 1
    ):
        return Suggestion(
            url, "tries", f"{url}|tries=2",
            f"{st.total_failures} isolated single-poll failure(s) triggering "
            f"{st.restarts_triggered} restart(s); tries=2 retries the blip before it "
            f"counts as a failure",
            st.restarts_triggered,
        )

    return None


def suggest_tunables(
    sites: dict[str, SiteStat], *, global_timeout: int, global_tries: int
) -> list[Suggestion]:
    """Per-URL tunable suggestions across all sites, ranked by restarts triggered.

    Pure function of the persisted stats — feed it a ``SiteStatsStore.sites`` map.
    Returns only sites worth acting on (healthy sites yield nothing), most-impactful
    first so the operator fixes the biggest restart driver before the long tail.
    """
    out = [
        s for url, st in sites.items()
        if (s := _suggest_one(url, st, global_timeout=global_timeout,
                              global_tries=global_tries)) is not None
    ]
    out.sort(key=lambda s: s.restarts, reverse=True)
    return out
