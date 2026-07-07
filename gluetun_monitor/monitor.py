"""The monitor loop — the ADR-0006 per-loop state machine (nodes 1-22).

Each loop: test gluetun's full URL set (root signal); restart + re-verify gluetun
if it breaches threshold; then — every loop, the core #20 fix — probe each
dependent (interface check + one shuffled viability name) and remediate the ones
that fail. All counter/state mutation is single-threaded; only the per-dependent
exec fan-out runs concurrently (bounded by MAX_PARALLEL_CHECKS).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .alert_state import AlertState
from .connectivity import probe_site
from .dependents import (
    classify_interfaces,
    discover_dependents,
    get_dependents,
    is_container_id,
    list_interfaces,
    parse_csv_names,
    remediation_action,
)
from .dns_check import DnsResult, DnsStatus, validate_dns
from .endpoint import get_endpoint_info
from .monitor_state import MonitorState
from .notify import Notifier, NotifyEvent, NullNotifier
from .recovery import RemediationOutcome, remediate_dependent, restart_gluetun
from .recreate import is_parked_name, sweep_recreate_leftovers
from .site_stats import SiteStatsStore, format_window
from .sites import (
    SiteSpec,
    hostname_of,
    ip_pool,
    is_ip_literal,
    load_specs,
    resolvable_pool,
    worst_case_probe_seconds,
)
from .state import Counter, InterfaceStatus, RemediationAction
from .tunables import suggest_tunables

if TYPE_CHECKING:
    from .config import Config
    from .docker_client import DockerClient
    from .logging_setup import Logger

Sleep = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class DependentProbe:
    """Read-only outcome of probing one dependent (no state mutation)."""

    name: str
    status: InterfaceStatus
    running: bool
    viability_ok: bool | None  # None = not tested (no pool / not live / unvalidatable)
    reason: str
    dns_unvalidated: bool = False  # True = no usable DNS tool in the container
    interfaces: str = "?"  # the dependent's net interfaces, for DEBUG visibility


@dataclass(slots=True)
class _WedgeTrack:
    """Consecutive-identical-remediation-failure bookkeeping for one dependent (#98).

    ``signature`` is the failure detail from the last attempt; a repeat with the
    SAME signature increments ``consecutive``, a different one starts the track
    over (a new failure is a new situation, not a deeper wedge). Once escalated,
    ``backoff_loops`` doubles per failed attempt (capped) and ``skip_remaining``
    counts down the loops until the next attempt — probing is never skipped,
    only the doomed remediation.
    """

    signature: str
    consecutive: int = 1
    escalated: bool = False
    backoff_loops: int = 0  # current retry delay, in loops
    skip_remaining: int = 0  # loops left before the next attempt
    body: str = ""  # last escalation-alert body, re-reported on skipped loops


def _fmt_reach(host: str, r: DnsResult) -> str:
    """Bare reach detail: ``<host> (<why>) [<tool>]`` (tool omitted if unknown)."""
    tool = f" [{r.tool}]" if r.tool else ""
    return f"{host} ({r.reason}){tool}"


def _link_line(status: str, interfaces: str) -> str:
    """L3 link verdict + interface list: ``live: eth0,lo,tun0`` / ``stranded: lo``
    / ``unknown`` (no list when the interfaces couldn't be read).
    """
    return f"{status}: {interfaces}" if interfaces and interfaces != "unknown" else status


class Monitor:
    """Owns the loop, the failure counters, and the shuffle RNG."""

    def __init__(
        self,
        client: DockerClient,
        config: Config,
        logger: Logger,
        *,
        rng: random.Random | None = None,
        sleep: Sleep = time.sleep,
        stats: SiteStatsStore | None = None,
        notifier: Notifier | None = None,
        state: MonitorState | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.log = logger
        # Opt-in external notifications (#22). Default = NullNotifier (no-op), so
        # nothing changes unless APPRISE_URLS is configured and a notifier injected.
        self.notifier = notifier if notifier is not None else NullNotifier()
        # Events buffered during one loop; flushed as a single rollup per cycle
        # (ADR-0011 — grouping is intrinsic, since our fast restarts can fire a
        # burst of events within one loop).
        self._pending: list[NotifyEvent] = []
        # Edge-triggered active-alert lifecycle (ADR-0012). Persisted only when
        # notifications are actually enabled (no stray sidecar for a log-only run).
        self.alerts = AlertState(
            config.notify_state_file if config.apprise_urls else None,
            logger=logger,
            repeat_interval=config.notify_repeat_interval,
        )
        self._rng = rng if rng is not None else random.Random()
        # _probe_dependent runs across a ThreadPoolExecutor (_fan_out), and a
        # random.Random instance is not thread-safe — concurrent sample()/uniform()
        # calls can interleave and corrupt its state, biasing the load-bearing site
        # shuffle (ADR-0006). All draws go through this lock so the shuffle stays
        # sound under fan-out. The lock is held only for the draw, never across I/O.
        self._rng_lock = threading.Lock()
        self._sleep = sleep
        # Persistent per-site stats (ADR-0008). Injectable for tests.
        self.stats = stats if stats is not None else SiteStatsStore(config.stats_file)
        self._advised_site: str | None = None  # dedup the flaky-site advisory
        # Sites that triggered an active gluetun-unrecovered alert (#106). Held so
        # the alert resolves only on OBSERVED recovery, not on the mechanical
        # post-restart counter reset that makes the next loop look sub-threshold.
        self._unrecovered_sites: set[str] = set()
        self.site_failures = Counter()
        self.dependent_failures = Counter()
        self._last_sites: set[str] | None = None
        # url -> SiteSpec for the current loop, so probe call sites can resolve a
        # per-URL timeout/tries override (#60). Refreshed each loop in _run_once_body
        # alongside the (deduplicated) URL list; empty until the first load.
        self._specs: dict[str, SiteSpec] = {}
        # Durable dependent memory (#97): gluetun id history + known dependent
        # names, persisted so neither a monitor restart nor a gluetun recreate
        # inside the first-loop window can blind remediation. Injectable for tests.
        self.mstate = state if state is not None else MonitorState(config.monitor_state_file)
        # Dependents seen at least once. A dependent stranded by a gluetun
        # *recreate* still points at the dead old id, so current-id discovery no
        # longer matches it — but we remember it from before the recreate and
        # keep checking it (ADR-0004: track across cycles). Pruned to existing.
        # Seeded from the persisted memory; _resolve_dependents prunes to existing.
        # Parked (*.gm-recreate-old) names are filtered even here: a state file
        # written by a version without the parked-name guard may carry them, and
        # they are recreate machinery, never dependents.
        self._known_dependents: set[str] = {
            n for n in self.mstate.known_dependents if not is_parked_name(n)
        }
        # The set managed last loop — to retire an alert ("no longer monitored") when
        # a dependent leaves (excluded or gone), rather than a false "resolved" (ADR-0012).
        self._managed_dependents: set[str] = set()
        # Dedup so a missing explicitly-listed dependent warns once, not per loop.
        self._warned_missing: set[str] = set()
        # Dedup for the dangling-orphan warning (see _scan_stranded_orphans).
        self._warned_orphans: set[str] = set()
        # Dedup for "can't validate DNS (no usable tool)" — re-armed if it later validates.
        self._warned_dns_unvalidated: set[str] = set()
        # Per-dependent wedge escalation + retry backoff (#98). In-memory only:
        # a restart mid-wedge re-detects within one attempt (the active
        # `dependent-wedged:` alert persists in the alert sidecar and is picked
        # up via alerts.is_active, so no re-announce and no false resolve).
        self._wedges: dict[str, _WedgeTrack] = {}

    def _notify(self, tier: str, title: str, body: str, key: str) -> None:
        """Buffer a one-shot point event for this loop's rollup (ADR-0011)."""
        self._pending.append(NotifyEvent(tier=tier, title=title, body=body, key=key))

    def _problem(self, key: str, tier: str, title: str, body: str) -> None:
        """Report an ONGOING problem this loop; the lifecycle (ADR-0012) edge-triggers
        the notification, repeats it per NOTIFY_REPEAT_INTERVAL, and resolves it.
        """
        self.alerts.report(key, tier, title, body)

    def _forget(self, key: str) -> None:
        """Retire a problem whose subject is no longer monitored — a "no longer
        monitored" deprecation notice, not a (false) "resolved" (ADR-0012).
        """
        self.alerts.forget(key)

    def _flush_notifications(self) -> None:
        """Emit this loop's lifecycle events + point events as one rollup, then persist."""
        events = self.alerts.events() + self._pending
        self._pending = []
        self.notifier.notify_batch(events)
        self.alerts.save()

    # ----- gluetun root test (nodes 2-3) -----

    def _probe_knobs(self, url: str) -> tuple[int, int]:
        """Effective (timeout, tries) for ``url``: its per-URL override if set,
        else the global ``TIMEOUT``/``WGET_TRIES`` (#60). A URL with no SiteSpec
        (e.g. an IP-literal pool entry, or one not in the current set) inherits the
        globals — so behavior is unchanged for un-annotated sites.
        """
        spec = self._specs.get(url)
        timeout = spec.timeout if spec and spec.timeout is not None else self.config.timeout
        tries = spec.tries if spec and spec.tries is not None else self.config.wget_tries
        return timeout, tries

    def _log_site_overrides(self) -> None:
        """Surface per-URL probe overrides so the operator sees what's in effect
        (#60) — INFO when any exist (discoverability, like the notify summary), a
        DEBUG confirmation of the global defaults otherwise.
        """
        overrides: list[str] = []
        for url in sorted(self._specs):
            spec = self._specs[url]
            knobs = [f"{k}={v}{u}" for k, v, u in (
                ("timeout", spec.timeout, "s"), ("tries", spec.tries, "")
            ) if v is not None]
            if knobs:
                overrides.append(f"{hostname_of(url)} {' '.join(knobs)}")
        defaults = f"TIMEOUT={self.config.timeout}s, WGET_TRIES={self.config.wget_tries}"
        if overrides:
            self.log.info(
                f"Per-URL probe overrides: {'; '.join(overrides)} (others use {defaults})"
            )
        else:
            self.log.debug(f"No per-URL probe overrides; all sites use {defaults}")

    def check_gluetun_sites(self, sites: list[str], *, record: bool = True) -> list[str]:
        """Test the full URL set from inside gluetun. Return the sites that have
        breached FAIL_THRESHOLD (empty = healthy).

        ``record`` updates the persistent per-site stats (ADR-0008); it's True for
        the primary loop test and False for the post-restart re-verify (so a loop
        counts as one poll per site, not two).
        """
        if not sites:
            self.log.warn("No sites configured to test")
            return []

        # Track the site *set* (not just the count) so a runtime sites.conf edit is
        # logged with the actual names — including a same-count swap, which a
        # count-only check would miss entirely. record=False is the post-restart
        # re-verify (same set), so skip the diff there.
        current = set(sites)
        if record and self._last_sites is None:
            self.log.info(f"Loaded {len(sites)} sites (sites.conf + SITES env)")
            self._log_site_overrides()
            self._last_sites = current
        elif record and current != self._last_sites:
            added = sorted(current - self._last_sites) if self._last_sites else sorted(current)
            removed = sorted(self._last_sites - current) if self._last_sites else []
            parts = []
            if added:
                parts.append(f"added {', '.join(added)}")
            if removed:
                parts.append(f"removed {', '.join(removed)}")
            self.log.info(f"Sites changed: {'; '.join(parts)} (now {len(sites)})")
            self._log_site_overrides()
            self._notify(
                "activity",
                "sites.conf changed",
                f"Test sites changed: {'; '.join(parts)} (now {len(sites)}).",
                key="sites-changed",
            )
            # A removed site keeps no live failure counter — a later re-add starts
            # clean rather than resuming near the threshold — and any active advisory
            # for it is retired with a "no longer monitored" notice (not a "resolved").
            for site in removed:
                self.site_failures.discard(site)
                self._forget(f"advisory:{site}")
            self._last_sites = current

        results = self._fan_out(
            sites, lambda url: probe_site(self.client, self.config.gluetun_container, url,
                                          *self._probe_knobs(url))
        )

        # Site tests run *inside the gluetun container* (the VPN gateway, through
        # the tunnel). Tag each line with the role + container — "[gateway:<name>]"
        # — so it's unambiguous what kind of container and which one (cf.
        # "[dependent:<name>]" below). Greppable: `grep gateway:` / `grep dependent:`.
        gw = f"gateway:{self.config.gluetun_container}"
        failed: list[str] = []
        for result in results:
            if record:
                self.stats.record_poll(result.url, result.ok, result.duration_ms, result.reason)
            if result.ok:
                self.site_failures.reset(result.url)
                self.log.debug(
                    f"[{gw}] reach ok: {result.url} ({result.reason}, {result.duration_ms}ms)"
                )
                continue
            count = self.site_failures.fail(result.url)
            threshold = self.config.fail_threshold
            if count >= threshold:
                failed.append(result.url)
                self.log.warn(
                    f"[{gw}] reach fail: {result.url} ({result.reason}) "
                    f"[{count}/{threshold} → restart]"
                )
            else:
                self.log.debug(
                    f"[{gw}] reach fail: {result.url} ({result.reason}) [{count}/{threshold}]"
                )

        if failed:
            self.log.error(f"[{gw}] restart triggered by: {' '.join(failed)}")

        # One-line INFO heartbeat so the default log level explains each loop
        # without the per-site DEBUG detail. Only on the primary poll (not the
        # post-restart re-verify, which has its own "verified" line).
        if record:
            ok_results = [r for r in results if r.ok]
            failing = [hostname_of(r.url) for r in results if not r.ok]
            if failing:
                self.log.info(
                    f"[{gw}] sites: {len(ok_results)}/{len(results)} ok — "
                    f"failing: {', '.join(failing)}"
                )
            else:
                slowest = max(results, key=lambda r: r.duration_ms)
                self.log.info(
                    f"[{gw}] sites: {len(ok_results)}/{len(results)} ok "
                    f"(slowest {slowest.duration_ms}ms: {hostname_of(slowest.url)})"
                )
        return failed

    # ----- dependent phase (nodes 6-19) -----

    def _probe_dependent(
        self, dep: str, gluetun_id: str, resolvable: list[str], ips: list[str]
    ) -> DependentProbe:
        """Classify + viability-test one dependent. Pure I/O, no state mutation.

        ``gluetun_id`` is the current gluetun container id, needed for the
        inspect-based fallback below.
        """
        # Optional per-dispatch jitter (default 0 = no-op) to de-sync the burst
        # of execs across live dependents (ADR-0006). The concurrency cap is the
        # primary bound; this only spreads start times when explicitly enabled.
        if self.config.max_jitter_ms > 0:
            with self._rng_lock:
                jitter = self._rng.uniform(0, self.config.max_jitter_ms) / 1000.0
            self._sleep(jitter)

        # L3/L4 interface check (node 7): which interfaces does the dependent have?
        # The set is captured (not just the verdict) so it's visible in DEBUG —
        # "can it route out" is exactly the eth0/tun0-vs-lo-only question.
        ifaces = list_interfaces(self.client, dep)
        status = classify_interfaces(ifaces)
        ifs = ",".join(sorted(ifaces)) if ifaces else "unknown"

        if status is InterfaceStatus.STRANDED:
            # Node 10: re-check once — a real strand won't self-heal.
            ifaces2 = list_interfaces(self.client, dep)
            if classify_interfaces(ifaces2) is InterfaceStatus.STRANDED:
                return DependentProbe(dep, status, False, None, "stranded loopback-only",
                                      interfaces=ifs)
            ifs2 = ",".join(sorted(ifaces2)) if ifaces2 else "unknown"
            return DependentProbe(dep, InterfaceStatus.LIVE, True, None, "recovered on re-check",
                                  interfaces=ifs2)

        if status is InterfaceStatus.UNKNOWN:
            # No shell to exec (distroless/scratch), so `ls /sys/class/net` can't
            # tell us anything. ADR-0004 designates inspect as the fallback signal
            # here: compare the dependent's NetworkMode target to gluetun's current
            # id. We act ONLY on the unambiguous moved-id verdict (RECREATE) — a
            # container that is Running but whose netns parent is a *different*
            # (dead) gluetun is stranded exactly as in #20, just invisible to the
            # interface check. A matching id (RESTART) or an unresolvable name-form
            # target (TRY_RESTART) is left alone: we can't prove a strand there and
            # must not churn a healthy container (Tenet 3). The id comparison is
            # stable (no flap), so unlike the interface path it needs no re-check.
            try:
                info = self.client.inspect(dep)
            except Exception as exc:
                # A transient Docker error (socket blip, APIError) on this ONE
                # dependent must not abort the whole fan-out and skip every other
                # dependent's remediation this loop. Skip it conservatively: report
                # it un-evaluated (running so it isn't remediated as "not running";
                # viability None so its alert is held, not resolved) and retry next
                # loop rather than churn a container we couldn't inspect (Tenet 3).
                self.log.debug(f"[dependent:{dep}] inspect failed ({exc}); skipped this loop")
                return DependentProbe(dep, InterfaceStatus.UNKNOWN, True, None,
                                      "inspect unavailable (transient)", interfaces=ifs)
            running = bool(info and info.running)
            if (
                info is not None
                and info.running
                and remediation_action(info, gluetun_id) is RemediationAction.RECREATE
            ):
                return DependentProbe(
                    dep, InterfaceStatus.STRANDED, True, None,
                    "inspect: netns id moved (gluetun recreated), container not exec'able",
                    interfaces=ifs,
                )
            return DependentProbe(dep, status, running, None, "link check unavailable",
                                  interfaces=ifs)

        # LIVE. The viability layer (L7 DNS + connectivity probe) is opt-out: with
        # it off, a live, non-stranded dependent is judged healthy on the interface
        # check alone — no URL fetch (ADR-0006; the interface check stays mandatory).
        if not self.config.dependent_viability:
            return DependentProbe(dep, status, True, None, "disabled (link check only)",
                                  interfaces=ifs)

        # Sample one (default) or more shuffled names per loop. The shuffle is
        # load-bearing (ADR-0006); DEPENDENT_VIABILITY_SAMPLES lets an operator
        # widen it (N, or -1 = all), at N execs/dependent/loop — usually redundant
        # since dependents share gluetun's netns (only DNS differs).
        pool = resolvable or ips
        if not pool:
            return DependentProbe(dep, status, True, None, "no test URLs", interfaces=ifs)
        n = self.config.dependent_viability_samples
        k = len(pool) if (n < 0 or n >= len(pool)) else max(1, n)
        with self._rng_lock:
            chosen = self._rng.sample(pool, k)

        # Only hostnames exercise DNS; IP literals have nothing to resolve.
        resolvable_chosen = [
            (u, hostname_of(u)) for u in chosen if not is_ip_literal(hostname_of(u))
        ]
        if not resolvable_chosen:
            return DependentProbe(dep, status, True, None,
                                  f"{chosen[0]} (IP literal — DNS not tested)", interfaces=ifs)

        # Validate the dependent's OWN DNS resolution (the only per-container fault
        # — dns_check). Viable if ANY sampled site resolves; not-viable only if
        # ALL sampled resolvable sites fail DNS; unvalidated if no tool exists.
        # ``reason`` is the bare detail — "<host> (<why>) [<tool>]" — because the
        # caller prefixes the verb + verdict ("reach ok/fail/?").
        results = [
            (h, validate_dns(self.client, dep, u, h, *self._probe_knobs(u)))
            for (u, h) in resolvable_chosen
        ]
        oks = [(h, r) for (h, r) in results if r.status is DnsStatus.OK]
        broken = [(h, r) for (h, r) in results if r.status is DnsStatus.BROKEN]
        if oks:
            h, r = oks[0]
            detail = (
                _fmt_reach(h, r) if len(results) == 1
                else f"{len(oks)}/{len(results)} sampled ok (e.g. {_fmt_reach(h, r)})"
            )
            return DependentProbe(dep, status, True, True, detail, interfaces=ifs)
        if broken:
            # Confirm-before-strike (#61): when we tested exactly ONE resolvable URL,
            # a single DNS-BROKEN can't tell a dead *domain* from a broken *resolver*.
            # Before counting a strike, draw one more *different* name and fail the
            # dependent only if that also fails DNS — so a dead site in the pool can't
            # by itself get a healthy container remediated (the 05:00-window false
            # positive). Eager-N sampling already tested several names, so this is a
            # no-op there (len(resolvable_chosen) > 1).
            fh, fr = broken[0]
            if len(resolvable_chosen) == 1:
                confirm = self._confirm_dns(dep, pool, exclude=resolvable_chosen[0][0])
                if confirm is not None:
                    ch, cr = confirm
                    if cr.status is DnsStatus.OK:
                        detail = (f"{_fmt_reach(fh, fr)} but {_fmt_reach(ch, cr)} "
                                  "— a dead site, not this container's DNS")
                        return DependentProbe(dep, status, True, True, detail, interfaces=ifs)
                    if cr.status is DnsStatus.BROKEN:
                        detail = (f"{_fmt_reach(fh, fr)}; confirmed via "
                                  f"{_fmt_reach(ch, cr)} — resolver broken")
                        return DependentProbe(dep, status, True, False, detail, interfaces=ifs)
                    # confirm UNVALIDATED (no usable tool) — keep the original verdict.
            detail = (
                _fmt_reach(fh, fr) if len(results) == 1
                else f"all {len(results)} sampled failed (e.g. {_fmt_reach(fh, fr)})"
            )
            return DependentProbe(dep, status, True, False, detail, interfaces=ifs)
        # All sampled sites were UNVALIDATED (no usable DNS tool in the container).
        h, r = results[0]
        return DependentProbe(dep, status, True, None, _fmt_reach(h, r),
                              dns_unvalidated=True, interfaces=ifs)

    def _confirm_dns(
        self, dep: str, pool: list[str], *, exclude: str
    ) -> tuple[str, DnsResult] | None:
        """Re-test one *different* resolvable name from ``pool`` to confirm a single
        DNS-BROKEN before it strikes (#61). Returns ``(host, result)``, or None when
        the pool has no other resolvable URL to confirm against (nothing to
        disambiguate — the caller keeps the original verdict). Bounded: exactly one
        extra probe, drawn through the shuffle lock like every other sample.
        """
        candidates = [
            u for u in pool if u != exclude and not is_ip_literal(hostname_of(u))
        ]
        if not candidates:
            return None
        with self._rng_lock:
            url = self._rng.choice(candidates)
        host = hostname_of(url)
        return host, validate_dns(
            self.client, dep, url, host, self.config.timeout, self.config.wget_tries
        )

    def _resolve_dependents(self) -> list[str]:
        """Current dependent set: discovery (or manual list) unioned with the
        remembered set, pruned to containers that still exist, minus EXCLUDE.

        A name that came from an explicit ``DEPENDENT_CONTAINERS`` list but does
        not exist is a likely misconfiguration — warn loudly (deduped) rather
        than silently dropping it. Auto-discovery never yields a missing name, so
        this only fires on the manual override. ``EXCLUDE_CONTAINERS`` is then
        subtracted: an excluded container is never managed, whatever the source.
        """
        current = get_dependents(self.client, self.config, self.log)
        present = {name: self.client.inspect(name) is not None for name in current}

        for name, exists in present.items():
            if not exists and name not in self._warned_missing:
                self.log.warn(f"Configured dependent '{name}' not found (DEPENDENT_CONTAINERS)")
                self._warned_missing.add(name)
        # Re-arm the warning for any name that has since reappeared.
        self._warned_missing &= {name for name, exists in present.items() if not exists}

        self._known_dependents.update(
            name for name, exists in present.items() if exists and not is_parked_name(name)
        )
        # Prune the remembered set to containers that still exist. Reuse the
        # existence we already learned for the current names; only inspect a
        # remembered name we haven't already checked this loop (e.g. one stranded
        # by a gluetun recreate, so current-id discovery no longer surfaces it).
        existence = dict(present)
        for d in self._known_dependents:
            if d not in existence:
                existence[d] = self.client.inspect(d) is not None
        self._known_dependents = {d for d in self._known_dependents if existence.get(d, False)}
        # Mirror the pruned remembered set into the durable memory (#97) so a
        # monitor restart resumes with exactly what this instance knew.
        self.mstate.set_known_dependents(self._known_dependents)
        excluded = set(parse_csv_names(self.config.exclude_containers))
        return sorted(self._known_dependents - excluded)

    def run_dependent_phase(self, gluetun_id: str, sites: list[str]) -> None:
        """Probe every dependent and remediate those that fail (nodes 6-19)."""
        dependents = self._resolve_dependents()
        # A dependent that left the managed set (excluded or gone) gets its active
        # alert retired with a "no longer monitored" notice, not a "resolved" that
        # would imply it recovered (ADR-0012).
        for gone in self._managed_dependents - set(dependents):
            self._forget(f"dependent-unhealthy:{gone}")
            self._forget(f"dependent-wedged:{gone}")
            self._wedges.pop(gone, None)
        self._managed_dependents = set(dependents)
        if not dependents:
            return

        resolvable = resolvable_pool(sites)
        ips = ip_pool(sites)
        if not resolvable and ips:
            self.log.warn(
                "No resolvable (hostname) test URLs — dependent DNS cannot be validated; "
                "testing connectivity only against IP literals"
            )

        probes = self._fan_out(
            dependents, lambda d: self._probe_dependent(d, gluetun_id, resolvable, ips)
        )

        # State mutation + remediation decisions are single-threaded. Each
        # dependent logs two ordered DEBUG lines: first the L3 network-path check
        # (which interfaces / can it route out), THEN the L7 viability result —
        # so the logs show the path was validated before the DNS/connect test.
        to_remediate: list[tuple[str, str]] = []
        # Dependents the loop could NOT evaluate this cycle (link/DNS couldn't tell):
        # an active alert for them must be HELD, not resolved, and their wedge kept.
        unevaluated: set[str] = set()
        for probe in probes:
            dep = probe.name
            tag = f"dependent:{dep}"  # role + name, mirrors "[gateway:<name>]"
            link = _link_line(probe.status.name.lower(), probe.interfaces)
            self.log.debug(f"[{tag}] link {link}")
            if probe.status is InterfaceStatus.STRANDED:
                to_remediate.append((dep, probe.reason))
                continue
            if probe.status is InterfaceStatus.UNKNOWN:
                self.log.debug(f"[{tag}] {probe.reason} (running={probe.running})")
                if not probe.running:
                    to_remediate.append((dep, "not running (link check unavailable)"))
                else:
                    unevaluated.add(dep)  # running but interfaces unreadable — not judged
                continue
            if probe.viability_ok is None:
                if probe.dns_unvalidated:
                    # Can't run any DNS tool in this container — say so loudly once
                    # (re-armed when it later validates), then rely on the link check.
                    if dep not in self._warned_dns_unvalidated:
                        self.log.warn(f"[{tag}] reach ?: {probe.reason}; using link check only")
                        self._warned_dns_unvalidated.add(dep)
                    else:
                        self.log.debug(f"[{tag}] reach ?: {probe.reason}")
                else:
                    self.log.debug(f"[{tag}] reach skip: {probe.reason}")
                unevaluated.add(dep)  # viability couldn't be determined — not judged
                continue  # not tested -> no counter change
            # A validated result (ok or broken): re-arm the unvalidated warning.
            self._warned_dns_unvalidated.discard(dep)
            if probe.viability_ok:
                self.dependent_failures.reset(dep)
                self.log.debug(f"[{tag}] reach ok: {probe.reason}")
                continue
            count = self.dependent_failures.fail(dep)
            threshold = self.config.dependent_container_failures
            if count >= threshold:
                to_remediate.append((dep, f"{count} consecutive viability failures"))
                self.log.warn(
                    f"[{tag}] reach fail: {probe.reason} [{count}/{threshold} → remediate]"
                )
            else:
                self.log.debug(f"[{tag}] reach fail: {probe.reason} [{count}/{threshold}]")

        # INFO heartbeat for the dependent phase (mirrors the gateway summary).
        healthy = len(probes) - len(to_remediate)
        if to_remediate:
            self.log.info(
                f"dependents: {healthy}/{len(probes)} ok — "
                f"remediating: {', '.join(dep for dep, _ in to_remediate)}"
            )
        else:
            self.log.info(f"dependents: {healthy}/{len(probes)} ok")

        # A dependent the loop couldn't evaluate wasn't proven healthy — hold any
        # active alert so it doesn't false-resolve mid-incident (e.g. all sites went
        # advisory → empty pool, or the container lost its DNS tool), and keep its
        # wedge track. Only a dependent seen genuinely healthy drops its bookkeeping.
        for dep in unevaluated:
            self.alerts.hold(f"dependent-unhealthy:{dep}")
            self.alerts.hold(f"dependent-wedged:{dep}")
        # A dependent that came back healthy WITHOUT our help (e.g. the operator
        # cleared the wedge and it recovered) drops its failure bookkeeping; its
        # wedged alert stops being reported, so the lifecycle resolves it.
        failing = {dep for dep, _ in to_remediate}
        for dep in [d for d in self._wedges if d not in failing and d not in unevaluated]:
            del self._wedges[dep]

        for dep, reason in to_remediate:
            if self.config.dry_run:
                # Observe-only: report the decision + the action we'd take, but
                # don't mutate. Counters are left intact (we didn't fix anything),
                # so persistent intent keeps surfacing each loop.
                info = self.client.inspect(dep)
                action = remediation_action(info, gluetun_id).name if info else "UNKNOWN"
                self.log.warn(f"[DRY-RUN] would remediate {dep}: {reason} (action={action})")
                continue
            track = self._wedges.get(dep)
            if track is not None and track.escalated and track.skip_remaining > 0:
                # Wedged and backing off: the same attempt has failed identically
                # every time — don't drive another doomed removal through the
                # daemon this loop. Keep the alert active (the lifecycle resolves
                # anything not re-reported) and keep probing every loop.
                track.skip_remaining -= 1
                self._problem(
                    f"dependent-wedged:{dep}", "attention",
                    f"dependent WEDGED: {dep}", track.body,
                )
                self.log.debug(
                    f"{dep} is wedged; backing off — next remediation attempt in "
                    f"{track.skip_remaining + 1} loop(s) (probes continue)"
                )
                continue
            self.log.warn(f"Remediating dependent {dep}: {reason}")
            self.stats.record_dependent_remediation()
            outcome = remediate_dependent(
                self.client, dep, gluetun_id, self.config, self.log, sleep=self._sleep
            )
            if outcome:
                self._wedges.pop(dep, None)
                self.dependent_failures.reset(dep)
                self._notify(
                    "recovery",
                    f"dependent recovered: {dep}",
                    f"{dep} was remediated ({reason}) and verified healthy.",
                    key=f"dependent-remediated:{dep}",
                )
            else:
                self._record_remediation_failure(dep, reason, outcome)

    def _record_remediation_failure(
        self, dep: str, reason: str, outcome: RemediationOutcome
    ) -> None:
        """Track consecutive identical remediation failures for ``dep``; escalate
        to a distinct wedged alert + retry backoff once the same failure has
        repeated WEDGE_ESCALATE_AFTER times (#98).

        Below the threshold this is exactly the pre-#98 behavior (the generic
        ``dependent-unhealthy`` lifecycle alert, retry next loop). A failure with
        a DIFFERENT signature restarts the count — a new error is a new
        situation, not a deeper wedge.
        """
        track = self._wedges.get(dep)
        if track is None or track.signature != outcome.detail:
            track = _WedgeTrack(signature=outcome.detail)
            self._wedges[dep] = track
        else:
            track.consecutive += 1
        # A wedged alert persisted from before a monitor restart means this
        # dependent was already escalated — stay escalated rather than demoting
        # to the generic alert (which would re-announce AND falsely resolve the
        # wedged one). report() on an active key refreshes silently.
        already = self.alerts.is_active(f"dependent-wedged:{dep}")
        if track.consecutive < self.config.wedge_escalate_after and not already:
            self._problem(
                f"dependent-unhealthy:{dep}",
                "attention",
                f"dependent unhealthy: {dep}",
                f"{dep} is still unhealthy after remediation ({reason}) — "
                f"manual intervention may be required.",
            )
            return
        first = not track.escalated
        track.escalated = True
        # Doubling backoff per failed attempt, in loops, capped. cap=0 disables
        # the backoff (attempt every loop) but keeps the escalated alert.
        cap_loops = self.config.wedge_backoff_cap // max(1, self.config.check_interval)
        track.backoff_loops = (
            min(max(2, track.backoff_loops * 2), cap_loops) if cap_loops > 0 else 0
        )
        track.skip_remaining = track.backoff_loops
        track.body = self._wedged_body(dep, track, outcome)
        if first:
            # The wedged alert supersedes the generic one: silently retire it (no
            # false "resolved" — the problem didn't clear, it got more specific).
            self.alerts.supersede(f"dependent-unhealthy:{dep}")
            self.log.warn(
                f"{dep} is WEDGED: the same remediation failure has repeated "
                f"{track.consecutive}x ({outcome.detail}) — operator action required; "
                f"backing off to one attempt every {track.backoff_loops or 1} loop(s) "
                f"(probes continue every loop)"
            )
        self._problem(
            f"dependent-wedged:{dep}", "attention",
            f"dependent WEDGED: {dep}", track.body,
        )

    def _wedged_body(self, dep: str, track: _WedgeTrack, outcome: RemediationOutcome) -> str:
        """The wedged-alert body: what is stuck, the exact error, and — when the
        blocker is an unremovable parked twin — the operator runbook, so the alert
        itself is enough to act on (#98).
        """
        retry_s = track.backoff_loops * self.config.check_interval
        cadence = (
            f"retries remediation only every ~{retry_s}s"
            if retry_s > 0
            else "keeps retrying every loop"
        )
        lines = [
            f"{dep} remediation has failed {track.consecutive} consecutive times with "
            f"the same error and cannot succeed without operator action. The monitor "
            f"keeps probing every loop but now {cadence}; it will finish the heal "
            f"itself once the blocker is cleared.",
            f"Error: {outcome.detail}",
        ]
        if outcome.wedge is not None:
            w = outcome.wedge
            lines += [
                f"Parked twin: {w.parked_name} (state: {w.parked_status}) — an "
                f"interrupted recreate's copy that Docker cannot remove; it shares "
                f"every volume with {dep}, so remediation is refused while it exists. "
                f"A 'dead' state with a busy-filesystem error usually means its "
                f"process tree is still alive and pinning the mount.",
                f"Runbook: 1) find the pinning processes — match the container's "
                f"storage/dataset id in /proc/*/mountinfo to LOCATE holders, but kill "
                f"only pids whose /proc/<pid>/cgroup contains this container's id "
                f"(host-namespace processes match mountinfo innocently); "
                f"2) docker rm -f {w.parked_name} (never remove the storage layer "
                f"directly — Docker owns it); 3) done — no monitor action needed.",
            ]
        return "\n".join(lines)

    # ----- one full loop iteration (node 1 -> 22, minus the sleep) -----

    def run_once(self) -> None:
        """Execute one monitoring cycle, then flush this loop's notification rollup.

        The flush is in ``finally`` so every early return (gluetun down, recovery
        failed, …) still emits the cycle's single digest (ADR-0011).
        """
        self._pending = []
        self.alerts.begin_loop()
        try:
            self._run_once_body()
        except Exception:
            # The loop died partway (e.g. the Docker socket went away mid-cycle):
            # subjects past the crash point were never evaluated, so the flush
            # below must not read their silence as recovery (#74).
            self.alerts.mark_incomplete()
            raise
        finally:
            self._flush_notifications()
            self.mstate.save()  # dirty-checked; no-op unless the memory changed

    def _run_once_body(self) -> None:
        """Execute one monitoring cycle (no inter-loop sleep)."""
        self.log.check("Start")
        self.stats.record_loop()  # monitor-wide: loop count + accumulated runtime

        gluetun = self.client.inspect(self.config.gluetun_container)
        if gluetun is None or not gluetun.running:
            self.log.error("Gluetun container is not running!")
            # The most severe condition must not be the quietest (#74): report it
            # to the lifecycle so it notifies (once, edge-triggered) and resolves
            # when gluetun returns. Nothing else was evaluated this loop, so the
            # other active alerts are held, not "resolved".
            self._problem(
                "gluetun-down",
                "attention",
                f"gluetun is down ({self.config.gluetun_container})",
                f"The gluetun container '{self.config.gluetun_container}' is "
                f"{'not running' if gluetun is not None else 'gone (not found)'} — "
                f"the monitor cannot check or heal the stack until it returns.",
            )
            self.alerts.mark_incomplete()
            return
        if gluetun.health != "healthy":
            self.log.warn(f"Gluetun health status: {gluetun.health}")
        # Remember every id gluetun runs under, and adopt any container stranded
        # on a dead FORMER gluetun id (#97) — before dependent resolution, so an
        # adopted orphan is probed and remediated in this same loop.
        self.mstate.record_gluetun_id(gluetun.id)
        self._scan_stranded_orphans(gluetun.id)

        # Observability before the site curls: log the gateway's own L3 interfaces
        # (symmetry with the dependent checks; presence of tun0 = tunnel up at L3).
        # We do NOT gate on this — the site tests are the authoritative tunnel
        # check (ADR-0001); this is just visibility.
        gw_ifaces = list_interfaces(self.client, self.config.gluetun_container)
        gw_ifs = ",".join(sorted(gw_ifaces)) if gw_ifaces else "unknown"
        self.log.debug(
            f"[gateway:{self.config.gluetun_container}] link "
            f"{_link_line(classify_interfaces(gw_ifaces).name.lower(), gw_ifs)}"
        )

        # Re-read every loop so editing sites.conf is picked up live (the SITES
        # env contribution is fixed at startup). Startup validation guarantees a
        # non-empty set; a runtime edit down to empty just tests nothing this loop.
        # Specs carry any per-URL |timeout/|tries overrides (#60); cache the url->spec
        # map so the probe call sites can resolve a site's effective knobs.
        # A failed reload (unreadable file, a parse bug) must never take the
        # watchdog's checks down with it: fall back to the last good set and
        # keep monitoring — a stale site list beats a silent halt (#73).
        try:
            specs = load_specs(self.config.config_file, self.config.sites_env)
            self._specs = {s.url: s for s in specs}
        except Exception as exc:
            self.log.error(
                f"Failed to reload sites config ({self.config.config_file}): {exc} "
                f"— keeping the previous {len(self._specs)} site(s)"
            )
            specs = list(self._specs.values())
        sites = [s.url for s in specs]
        # A per-URL |timeout= override can push a probe's legitimate runtime past
        # the transport read-timeout sized from the globals — the probe would be
        # killed by the transport and a slow SUCCESS misreported as a failure
        # (#77). Track the worst case on every reload; raise-only, so a config
        # edit can never clip a probe already in flight.
        worst = worst_case_probe_seconds(specs, self.config.timeout, self.config.wget_tries)
        self.client.ensure_timeout(max(worst * 2, 60))

        breached = self.check_gluetun_sites(sites)
        if not breached:
            # Keep the flaky-site advisory current on HEALTHY loops too (#75):
            # the lifecycle resolves any alert not re-reported each loop, so
            # evaluating only on breach loops made the advisory flap — a false
            # "resolved" on the first healthy loop after every breach, then a
            # fresh announce on the next one. Evaluated every loop, the alert
            # stays active until the dominance window genuinely clears.
            self._emit_advisory()
            # #106: a failed recovery zeroes every site counter, so the very next
            # loop is sub-threshold ("not breached") even while the triggering site
            # is still down. That mechanical dip used to auto-resolve an active
            # gluetun-unrecovered alert — a false "recovered". Hold it until the
            # sites that triggered it have OBSERVABLY cleared this loop.
            failing_now = {url for url in sites if self.site_failures.get(url) > 0}
            self._reconcile_unrecovered(failing_now)
            # Gluetun is up — proceed straight to the dependent phase (the #20 fix:
            # dependents are checked every loop, not only after a gluetun failure).
            self.run_dependent_phase(gluetun.id, sites)
            self._save_stats()
            return

        # Gluetun breached threshold: restart + re-verify before touching dependents.
        self.log.warn("Gluetun unhealthy → restarting")
        # Attribute the restart to the breached site(s) and surface a flaky-site
        # advisory if one site keeps causing them (ADR-0008). Persist now — a
        # restart is an important, infrequent event worth not losing.
        for site in breached:
            self.stats.record_restart(site)
        self._emit_advisory()
        self._save_stats()

        if self.config.dry_run:
            # Observe-only: don't restart gluetun (can't, without mutating). Log
            # the intent and still probe dependents so their decisions are visible.
            self.log.warn(
                "[DRY-RUN] would restart gluetun and re-verify before touching "
                "dependents; skipping (observe-only)"
            )
            self.run_dependent_phase(gluetun.id, sites)
            return
        self.stats.record_gluetun_restart()  # monitor-wide count of actual restarts
        self._notify(
            "all",
            f"gluetun restart triggered ({self.config.gluetun_container})",
            f"Connectivity failed for: {', '.join(breached)} — restarting and re-verifying.",
            key="gluetun-restart",
        )
        if not restart_gluetun(self.client, self.config, self.log, sleep=self._sleep):
            self.log.error("Recovery failed - manual intervention may be required")
            self._problem(
                "gluetun-unrecovered",
                "attention",
                f"gluetun cannot recover ({self.config.gluetun_container})",
                f"Was failing on {', '.join(breached)}; restarted but it did not come back "
                f"healthy — manual intervention may be required.",
            )
            self._unrecovered_sites = set(breached)  # #106: hold until these clear
            self.alerts.mark_incomplete()  # dependents were never evaluated (#74)
            return

        # Re-verify without recording (so the loop counts one poll per site, not two).
        reverify_breached = self.check_gluetun_sites(sites, record=False)
        # Restart effectiveness: did each site that triggered this restart recover?
        still = set(reverify_breached)
        for site in breached:
            self.stats.record_restart_outcome(site, cleared=site not in still)
        if reverify_breached:
            self.log.warn("Connectivity still failing after restart; leaving dependents untouched")
            self._problem(
                "gluetun-unrecovered",
                "attention",
                f"gluetun cannot recover ({self.config.gluetun_container})",
                f"Restarted {self.config.gluetun_container} but it's still failing: "
                f"{', '.join(reverify_breached)} — manual intervention may be required.",
            )
            self._unrecovered_sites = set(reverify_breached)  # #106: hold until these clear
            self.site_failures.reset_all()
            self.alerts.mark_incomplete()  # dependents left untouched = unevaluated (#74)
            return

        self.log.info("Connectivity verified after restart")
        self._notify(
            "recovery",
            f"gluetun recovered ({self.config.gluetun_container})",
            f"Was failing on {', '.join(breached)}; restarted and connectivity is healthy again.",
            key="gluetun-recovered",
        )
        self._unrecovered_sites = set()  # #106: observed recovery — let the alert resolve
        self.site_failures.reset_all()
        # Re-inspect: a restart keeps the same id, but be robust if it was recreated.
        gluetun = self.client.inspect(self.config.gluetun_container) or gluetun
        self.run_dependent_phase(gluetun.id, sites)
        self._save_stats()

    def _reconcile_unrecovered(self, failing_now: set[str]) -> None:
        """Hold or resolve the ``gluetun-unrecovered`` alert from OBSERVED recovery
        rather than the mechanical post-restart counter reset (#106).

        The failed-recovery path zeroes every site counter, so the loop right after
        it is always sub-threshold — which let :class:`AlertState` auto-resolve the
        alert (a false "recovered") while the triggering site was still failing.
        Called on every "not breached" loop: re-report the alert while its
        triggering sites are still failing so it stays active, and let it resolve
        only when they have genuinely cleared. Symmetric with ``_emit_advisory``,
        which #75 hardened the same way.
        """
        if not self.alerts.is_active("gluetun-unrecovered"):
            self._unrecovered_sites = set()
            return
        # Restart-safe: the alert can outlive the process (persisted sidecar), but
        # the in-RAM triggering set does not. If it survived a monitor restart,
        # adopt whatever is failing right now as the condition to hold on — a
        # genuinely clean loop still resolves it below.
        if not self._unrecovered_sites:
            self._unrecovered_sites = set(failing_now)
        remaining = self._unrecovered_sites & failing_now
        if remaining:
            self._problem(
                "gluetun-unrecovered",
                "attention",
                f"gluetun cannot recover ({self.config.gluetun_container})",
                f"Restarted {self.config.gluetun_container} but it's still failing: "
                f"{', '.join(sorted(remaining))} — manual intervention may be required.",
            )
        else:
            # The sites that triggered it are passing again → observed recovery.
            # Not re-reporting lets the lifecycle emit the (true) resolve.
            self._unrecovered_sites = set()

    def _emit_advisory(self) -> None:
        """Surface a flaky-site advisory when one site dominates recent restarts.

        Called every loop — healthy and breached (#75) — so the alert lifecycle
        sees the advisory continuously while the dominance window holds: the
        notification edge-triggers once and resolves only when the window
        genuinely clears, instead of flapping around each breach. The log line +
        stats count still fire once per episode (``_advised_site``).
        """
        adv = self.stats.advisory(
            self.config.advisory_window,
            self.config.advisory_min_restarts,
            self.config.advisory_dominance,
        )
        if adv is None:
            self._advised_site = None
            return
        # Make the advisory actionable: if the stats support a specific per-URL knob
        # (e.g. the site is slow-but-alive → a wider timeout), name it (#60) instead
        # of only "consider removing it".
        hint = self._advisory_hint(adv.site)
        self._problem(
            f"advisory:{adv.site}",
            "attention",
            f"flaky site: {adv.site}",
            f"{adv.site} caused {adv.site_restarts} of the last {adv.total_restarts} "
            f"gluetun restarts over {format_window(adv.window_seconds)} — it may be flaky; "
            f"consider reviewing or removing it from sites.conf.{hint}",
        )
        if adv.site == self._advised_site:
            return  # log + stats once per episode (the notification is deduped by AlertState)
        self._advised_site = adv.site
        self.stats.record_advisory()
        self.log.warn(
            f"FLAKY SITE: {adv.site} caused {adv.site_restarts} of the last "
            f"{adv.total_restarts} gluetun restarts over the last "
            f"{format_window(adv.window_seconds)} — it may be flaky; consider "
            f"reviewing or removing it from sites.conf.{hint}"
        )

    def _advisory_hint(self, site: str) -> str:
        """A one-sentence per-URL tunable suggestion for ``site`` if the stats
        support a concrete knob, else "" (#60). Appended to the flaky-site advisory
        so the operator gets the specific fix, not just "consider removing it".
        """
        st = self.stats.sites.get(site)
        if st is None:
            return ""
        sugg = suggest_tunables(
            {site: st}, global_timeout=self.config.timeout,
            global_tries=self.config.wget_tries,
            specs=self._specs,  # judge against the site's EFFECTIVE knobs (#77)
        )
        if sugg and sugg[0].config_line:
            return (f" The stats suggest `{sugg[0].config_line}` — run "
                    "--suggest-tunables for the reasoning.")
        return ""

    def _save_stats(self) -> None:
        """Prune stale sites (retention), then persist — every loop (best-effort;
        a few-KB atomic write, so no throttle needed).
        """
        pruned = self.stats.prune_stale(self.config.stats_retention_days * 86400)
        if pruned:
            self.log.info(
                f"Pruned {len(pruned)} stale site stat(s) not seen in "
                f"{self.config.stats_retention_days}d: {', '.join(pruned)}"
            )
        self.stats.save()

    def announce(self) -> None:
        """Log the post-prerequisite startup context (connection, dependents, endpoint)."""
        # Before anything else, heal any recreate that a previous monitor died in
        # the middle of (SIGKILL/power loss) — restore or retire the parked
        # container (#76), so discovery below sees the true state.
        sweep_recreate_leftovers(self.client, self.log)
        if self.config.docker_host:
            self.log.info(f"Docker connection: socket proxy ({self.config.docker_host})")
        else:
            self.log.info("Docker connection: local socket")
        if self.config.dependent_containers == "auto":
            discovered = discover_dependents(self.client, self.config.gluetun_container)
            if discovered:
                self.log.info(f"Dependent containers (auto-discovery): {','.join(discovered)}")
            else:
                self.log.info("Dependent containers: auto-discovery enabled (none found currently)")
        else:
            self.log.info(f"Dependent containers (manual): {self.config.dependent_containers}")
        excluded = parse_csv_names(self.config.exclude_containers)
        if excluded:
            self.log.info(f"Excluded from management (EXCLUDE_CONTAINERS): {','.join(excluded)}")
        # Seed the id history with the current gluetun and adopt anything already
        # stranded (#97) BEFORE the first loop, so a recreate that happened while
        # the monitor was down is healed immediately rather than never seen.
        gluetun_info = self.client.inspect(self.config.gluetun_container)
        if gluetun_info is not None:
            self.mstate.record_gluetun_id(gluetun_info.id)
        self._scan_stranded_orphans(gluetun_info.id if gluetun_info is not None else None)
        self.mstate.save()
        startup = get_endpoint_info(self.client, self.config.gluetun_container)
        self.log.endpoint(startup.format("STARTUP", "Monitor starting"))

    def _scan_stranded_orphans(self, current_gluetun_id: str | None) -> None:
        """Adopt containers stranded on a dead FORMER gluetun id; warn about the
        rest (#97). Also reference-prunes the persisted gluetun id history.

        A container — running *or* exited — whose ``NetworkMode`` points at a
        dead id is stranded. If that id is in the persisted gluetun history it
        provably shared OUR gluetun's netns (Docker ids are never reused), so it
        is adopted into the remembered set and healed through the normal
        remediation path. A dead parent NOT in the history cannot be attributed
        to this gluetun (it might belong to some other netns owner), so acting
        on it could re-home the wrong container (Tenet 1) — those get the
        pre-#97 behavior: a once-per-name warning suggesting
        ``DEPENDENT_CONTAINERS``, and only when running (an exited container on
        an unknown dead parent is ordinary leftover junk, not a page).

        The scan must include exited containers: a stranded dependent's own
        restart policy usually drives it to Exited (a start onto a dead netns id
        fails hard) — the running-only view would hide exactly the containers
        most in need of healing.
        """
        gluetun = self.config.gluetun_container
        listed = (
            set()
            if self.config.dependent_containers == "auto"
            else set(parse_csv_names(self.config.dependent_containers))
        )
        excluded = set(parse_csv_names(self.config.exclude_containers))
        skip = listed | excluded | {gluetun} | self._known_dependents
        # Every netns-parent id any existing container references: the keep-set
        # for reference-based pruning of the id history (an id stays exactly as
        # long as it can still matter to an adoption decision).
        referenced: set[str] = set()
        if current_gluetun_id:
            referenced.add(current_gluetun_id)
        for cid in self.client.list_all_ids():
            info = self.client.inspect(cid)
            if info is None:
                continue
            nm = info.network_mode
            if not nm.startswith("container:"):
                continue
            target = nm.split(":", 1)[1]
            if not is_container_id(target):
                continue  # name-form target resolves normally; not a dangling id
            referenced.add(target)
            if info.name in skip:
                continue
            if is_parked_name(info.name):
                # Recreate machinery (gc/sweep territory) — adopting a parked
                # twin would re-park it under a stacked suffix and mint a fresh
                # orphan every loop (found by the #97 dogfood).
                continue
            if self.client.inspect(target) is not None:
                continue  # the netns parent still exists — not stranded
            if self.mstate.is_former_gluetun(target):
                self.log.warn(
                    f"Adopting stranded dependent '{info.name}': its network parent "
                    f"({target[:12]}) was a previous {gluetun} container — it will be "
                    f"remediated this loop"
                )
                self._known_dependents.add(info.name)
                self._warned_orphans.discard(info.name)
            elif info.running and info.name not in self._warned_orphans:
                self.log.warn(
                    f"Container '{info.name}' is running but its network parent "
                    f"({target[:12]}) no longer exists; if it depends on gluetun, add it "
                    f"to DEPENDENT_CONTAINERS so it can be healed (not auto-recreated — "
                    f"its parent can't be confirmed as gluetun)"
                )
                self._warned_orphans.add(info.name)
        self.mstate.prune_gluetun_ids(referenced)

    def run(self) -> None:
        """Run forever: loop run_once + sleep CHECK_INTERVAL."""
        while True:
            try:
                self.run_once()
            except Exception as exc:  # never let one bad loop kill the monitor (ROC)
                self.log.error(f"Unhandled error in monitor loop: {exc}")
            self.log.check(f"End - Sleeping {self.config.check_interval}s")
            self._sleep(self.config.check_interval)

    # ----- helpers -----

    def _fan_out[T, R](self, items: list[T], fn: Callable[[T], R]) -> list[R]:
        """Run ``fn`` over ``items`` with a bounded thread pool, preserving order."""
        workers = max(1, min(self.config.max_parallel_checks, len(items)))
        if workers == 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, items))
