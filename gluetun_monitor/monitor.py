"""The monitor loop — the ADR-0006 per-loop state machine (nodes 1-22).

Each loop: test gluetun's full URL set (root signal); restart + re-verify gluetun
if it breaches threshold; then — every loop, the core #20 fix — probe each
dependent (interface check + one shuffled viability name) and remediate the ones
that fail. All counter/state mutation is single-threaded; only the per-dependent
exec fan-out runs concurrently (bounded by MAX_PARALLEL_CHECKS).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .connectivity import probe_site
from .dependents import (
    discover_dependents,
    get_dependents,
    interface_check,
    is_container_id,
    parse_csv_names,
    remediation_action,
)
from .endpoint import get_endpoint_info
from .recovery import remediate_dependent, restart_gluetun
from .sites import ip_pool, load_sites, resolvable_pool
from .state import Counter, InterfaceStatus, RemediationAction

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
    viability_ok: bool | None  # None = not tested (no pool / not live)
    reason: str


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
    ) -> None:
        self.client = client
        self.config = config
        self.log = logger
        self._rng = rng if rng is not None else random.Random()
        self._sleep = sleep
        self.site_failures = Counter()
        self.dependent_failures = Counter()
        self._last_site_count: int | None = None
        # Dependents seen at least once. A dependent stranded by a gluetun
        # *recreate* still points at the dead old id, so current-id discovery no
        # longer matches it — but we remember it from before the recreate and
        # keep checking it (ADR-0004: track across cycles). Pruned to existing.
        self._known_dependents: set[str] = set()
        # Dedup so a missing explicitly-listed dependent warns once, not per loop.
        self._warned_missing: set[str] = set()
        # Dedup for the dangling-orphan warning (see _warn_dangling_orphans).
        self._warned_orphans: set[str] = set()

    # ----- gluetun root test (nodes 2-3) -----

    def check_gluetun_sites(self, sites: list[str]) -> bool:
        """Test the full URL set from inside gluetun. True if none breached threshold."""
        if not sites:
            self.log.warn("No sites configured to test")
            return True

        if self._last_site_count != len(sites):
            if self._last_site_count is None:
                self.log.info(f"Loaded {len(sites)} sites (sites.conf + SITES env)")
            else:
                self.log.info(f"Site count changed from {self._last_site_count} to {len(sites)}")
            self._last_site_count = len(sites)

        results = self._fan_out(
            sites, lambda url: probe_site(self.client, self.config.gluetun_container, url,
                                          self.config.timeout)
        )

        failed: list[str] = []
        for result in results:
            if result.ok:
                self.site_failures.reset(result.url)
                self.log.debug(f"Site {result.url} passed ({result.duration_ms}ms)")
                continue
            count = self.site_failures.fail(result.url)
            if count >= self.config.fail_threshold:
                failed.append(result.url)
                self.log.warn(
                    f"Site {result.url} failed {count} consecutive times - "
                    f"THRESHOLD REACHED - {result.reason}"
                )
            else:
                remaining = self.config.fail_threshold - count
                self.log.debug(
                    f"Site {result.url} failed ({count}/{self.config.fail_threshold}) - "
                    f"{remaining} more to trigger restart - {result.reason}"
                )

        if failed:
            self.log.error(f"Failed sites (exceeded threshold): {' '.join(failed)}")
            return False
        return True

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
            self._sleep(self._rng.uniform(0, self.config.max_jitter_ms) / 1000.0)

        status = interface_check(self.client, dep)

        if status is InterfaceStatus.STRANDED:
            # Node 10: re-check once — a real strand won't self-heal.
            if interface_check(self.client, dep) is InterfaceStatus.STRANDED:
                return DependentProbe(dep, status, False, None, "stranded loopback-only")
            return DependentProbe(dep, InterfaceStatus.LIVE, True, None, "recovered on re-check")

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
            info = self.client.inspect(dep)
            running = bool(info and info.running)
            if (
                running
                and info is not None
                and remediation_action(info, gluetun_id) is RemediationAction.RECREATE
            ):
                return DependentProbe(
                    dep,
                    InterfaceStatus.STRANDED,
                    True,
                    None,
                    "inspect: netns id moved (gluetun recreated), container not exec'able",
                )
            return DependentProbe(dep, status, running, None, "interface check unavailable")

        # LIVE. The viability layer (L7 DNS + connectivity probe) is opt-out: with
        # it off, a live, non-stranded dependent is judged healthy on the interface
        # check alone — no URL fetch (ADR-0006; the interface check stays mandatory).
        if not self.config.dependent_viability:
            return DependentProbe(dep, status, True, None, "viability disabled (interface only)")

        # one shuffled name per loop (the shuffle is load-bearing, ADR-0006).
        pool = resolvable or ips
        if not pool:
            return DependentProbe(dep, status, True, None, "no test URLs")
        url = self._rng.choice(pool)
        result = probe_site(self.client, dep, url, self.config.timeout)
        return DependentProbe(dep, status, True, result.ok, f"{url}: {result.reason}")

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

        self._known_dependents.update(name for name, exists in present.items() if exists)
        self._known_dependents = {
            d for d in self._known_dependents if self.client.inspect(d) is not None
        }
        excluded = set(parse_csv_names(self.config.exclude_containers))
        return sorted(self._known_dependents - excluded)

    def run_dependent_phase(self, gluetun_id: str, sites: list[str]) -> None:
        """Probe every dependent and remediate those that fail (nodes 6-19)."""
        dependents = self._resolve_dependents()
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

        # State mutation + remediation decisions are single-threaded.
        to_remediate: list[tuple[str, str]] = []
        for probe in probes:
            if probe.status is InterfaceStatus.STRANDED:
                to_remediate.append((probe.name, probe.reason))
                continue
            if probe.status is InterfaceStatus.UNKNOWN:
                if not probe.running:
                    to_remediate.append((probe.name, "not running (interface check unavailable)"))
                continue
            if probe.viability_ok is None:
                continue  # nothing to test
            if probe.viability_ok:
                self.dependent_failures.reset(probe.name)
                self.log.debug(f"Dependent {probe.name}: {probe.reason} [fails 0]")
                continue
            count = self.dependent_failures.fail(probe.name)
            threshold = self.config.dependent_container_failures
            if count >= threshold:
                to_remediate.append(
                    (probe.name, f"{count} consecutive viability failures")
                )
                self.log.warn(
                    f"Dependent {probe.name}: {probe.reason} "
                    f"[fails {count}/{threshold} -> remediate]"
                )
            else:
                self.log.debug(
                    f"Dependent {probe.name}: {probe.reason} [fails {count}/{threshold}]"
                )

        for dep, reason in to_remediate:
            if self.config.dry_run:
                # Observe-only: report the decision + the action we'd take, but
                # don't mutate. Counters are left intact (we didn't fix anything),
                # so persistent intent keeps surfacing each loop.
                info = self.client.inspect(dep)
                action = remediation_action(info, gluetun_id).name if info else "UNKNOWN"
                self.log.warn(f"[DRY-RUN] would remediate {dep}: {reason} (action={action})")
                continue
            self.log.warn(f"Remediating dependent {dep}: {reason}")
            if remediate_dependent(
                self.client, dep, gluetun_id, self.config, self.log, sleep=self._sleep
            ):
                self.dependent_failures.reset(dep)

    # ----- one full loop iteration (node 1 -> 22, minus the sleep) -----

    def run_once(self) -> None:
        """Execute one monitoring cycle (no inter-loop sleep)."""
        self.log.check("Start")

        gluetun = self.client.inspect(self.config.gluetun_container)
        if gluetun is None or not gluetun.running:
            self.log.error("Gluetun container is not running!")
            return
        if gluetun.health != "healthy":
            self.log.warn(f"Gluetun health status: {gluetun.health}")

        # Re-read every loop so editing sites.conf is picked up live (the SITES
        # env contribution is fixed at startup). Startup validation guarantees a
        # non-empty set; a runtime edit down to empty just tests nothing this loop.
        sites = load_sites(self.config.config_file, self.config.sites_env)

        if self.check_gluetun_sites(sites):
            # Gluetun is up — proceed straight to the dependent phase (the #20 fix:
            # dependents are checked every loop, not only after a gluetun failure).
            self.run_dependent_phase(gluetun.id, sites)
            return

        # Gluetun breached threshold: restart + re-verify before touching dependents.
        self.log.warn("Health check failed, initiating recovery...")
        if self.config.dry_run:
            # Observe-only: don't restart gluetun (can't, without mutating). Log
            # the intent and still probe dependents so their decisions are visible.
            self.log.warn(
                "[DRY-RUN] would restart gluetun and re-verify before touching "
                "dependents; skipping (observe-only)"
            )
            self.run_dependent_phase(gluetun.id, sites)
            return
        if not restart_gluetun(self.client, self.config, self.log, sleep=self._sleep):
            self.log.error("Recovery failed - manual intervention may be required")
            return

        if not self.check_gluetun_sites(sites):
            self.log.warn("Connectivity still failing after restart; leaving dependents untouched")
            self.site_failures.reset_all()
            return

        self.log.info("Connectivity verified after restart")
        self.site_failures.reset_all()
        # Re-inspect: a restart keeps the same id, but be robust if it was recreated.
        gluetun = self.client.inspect(self.config.gluetun_container) or gluetun
        self.run_dependent_phase(gluetun.id, sites)

    def announce(self) -> None:
        """Log the post-prerequisite startup context (connection, dependents, endpoint)."""
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
        self._warn_dangling_orphans()
        startup = get_endpoint_info(self.client, self.config.gluetun_container)
        self.log.endpoint(startup.format("STARTUP", "Monitor starting"))

    def _warn_dangling_orphans(self) -> None:
        """Surface a running container stranded on a *dead* netns parent that we
        are not managing — most likely a gluetun dependent that was recreate-
        stranded before the monitor started (its NetworkMode still points at
        gluetun's old, now-gone id, so current-id discovery can't see it).

        We *warn and suggest* DEPENDENT_CONTAINERS rather than recreate it: an
        orphan whose parent is gone can't be confidently attributed to *this*
        gluetun (it might belong to some other netns owner), and acting on that
        guess could re-home or churn the wrong container (Tenet 1 — first, do no
        harm). Names already listed or excluded are skipped (we either manage them
        or were told not to touch them).
        """
        gluetun = self.config.gluetun_container
        listed = (
            set()
            if self.config.dependent_containers == "auto"
            else set(parse_csv_names(self.config.dependent_containers))
        )
        skip = listed | set(parse_csv_names(self.config.exclude_containers)) | {gluetun}
        for cid in self.client.list_running_ids():
            info = self.client.inspect(cid)
            if info is None or info.name in skip:
                continue
            nm = info.network_mode
            if not nm.startswith("container:"):
                continue
            target = nm.split(":", 1)[1]
            if not is_container_id(target):
                continue  # name-form target resolves normally; not a dangling id
            if self.client.inspect(target) is not None:
                continue  # the netns parent still exists — not stranded
            if info.name not in self._warned_orphans:
                self.log.warn(
                    f"Container '{info.name}' is running but its network parent "
                    f"({target[:12]}) no longer exists; if it depends on gluetun, add it "
                    f"to DEPENDENT_CONTAINERS so it can be healed (not auto-recreated — "
                    f"its parent can't be confirmed as gluetun)"
                )
                self._warned_orphans.add(info.name)

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
