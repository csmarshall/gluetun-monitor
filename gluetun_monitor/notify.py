"""Opt-in external notification layer (issue #22, ADR-0010 + ADR-0011).

By default (``APPRISE_URLS`` unset) this is a :class:`NullNotifier`: zero behavior
change — the monitor stays a log-only tool. When configured, events are pushed
out-of-band via `Apprise <https://github.com/caronc/apprise>`_ (100+ backends).

Two design decisions live here (ADR-0011):

* **One dial, keyed on actionability** — ``NOTIFY_LEVEL`` is a cumulative tier
  floor, default ``action``: ``action`` (you must act) → ``recovery`` (self-healed
  incidents) → ``activity`` (non-fault changes) → ``all`` (firehose).
* **Rollup is the substrate** — the notifier collects a loop's events, filters by
  the tier floor, and emits **one** digest notification (colored by the worst tier
  present). A single event is sent as itself; the one-shot CLI paths are a batch of
  one. Grouping is how it works, not a toggle.

Re-notification of ongoing problems (edge-trigger, repeat, resolve) lives in
:class:`~gluetun_monitor.alert_state.AlertState` (ADR-0012), upstream of here.

Tenet 7 (best-effort, non-blocking): every send is filtered by tier, run off the
loop on a bounded daemon thread (apprise's ``notify`` is synchronous), and on any
failure swallowed + logged at DEBUG. Apprise URLs carry secrets and are never logged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .config import Config
    from .logging_setup import Logger

# Actionability tiers, quietest floor → loudest. A cumulative NOTIFY_LEVEL of rank
# R sends every event whose tier rank is <= R.
TIERS = ("attention", "recovery", "activity", "all")
_TIER_RANK = {name: i for i, name in enumerate(TIERS)}
# Tier → Apprise notify-type (color/icon). Strings are accepted directly (apprise's
# NotifyType is str-based; the real-apprise CI test confirms this).
_TIER_NOTIFY_TYPE = {"attention": "failure", "recovery": "success", "activity": "info", "all": "info"}


def tier_rank(name: str) -> int:
    """Rank of a tier name (0 = action … 3 = all); unknown → 0 (the loud floor)."""
    return _TIER_RANK.get(name.lower(), 0)


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """One notifiable occurrence.

    ``tier`` selects the actionability rung (see ``TIERS``). ``key`` is the stable
    identity of the thing being reported (used by the lifecycle, ADR-0012).
    """

    tier: str
    title: str
    body: str
    key: str


class Notifier(Protocol):
    """Sink for :class:`NotifyEvent`. Implementations must never raise."""

    def notify_batch(self, events: list[NotifyEvent]) -> None:
        """Filter a loop's events by tier and emit one rollup."""
        ...

    def notify(self, event: NotifyEvent) -> None:
        """Send a single event (a batch of one) — for one-shot paths."""
        ...

    def test(self) -> bool:
        """Send a test notification; return True if it was delivered."""
        ...


class NullNotifier:
    """The default when ``APPRISE_URLS`` is unset — notifications disabled."""

    def notify_batch(self, events: list[NotifyEvent]) -> None:
        """No-op."""
        return

    def notify(self, event: NotifyEvent) -> None:
        """No-op."""
        return

    def test(self) -> bool:
        """Nothing configured to test."""
        return False


class AppriseNotifier:
    """:class:`Notifier` over Apprise: tier filter + per-loop rollup, fully swallowed.

    Re-notification of ongoing problems (edge-trigger, repeat, resolve) lives in
    :class:`~gluetun_monitor.alert_state.AlertState`, so this stays a dumb sink:
    filter by tier, group into one digest, send. ``apprise_factory`` is injectable so
    the filter/rollup logic is unit-testable with no library and no network;
    production passes ``None`` and the real ``apprise`` is imported lazily.
    """

    def __init__(
        self,
        urls: tuple[str, ...],
        *,
        level: str,
        logger: Logger,
        timeout_seconds: int = 10,
        apprise_factory: Callable[[tuple[str, ...]], Any] | None = None,
    ) -> None:
        self._level = tier_rank(level)
        self._timeout = timeout_seconds
        self._log = logger
        self._apprise = self._build(urls, apprise_factory)
        # At most ONE send in flight: a backend that ignores its own socket timeout
        # would otherwise leak one abandoned daemon thread per loop, unbounded (#85).
        self._inflight: threading.Thread | None = None

    def _build(self, urls: tuple[str, ...], factory: Callable[[tuple[str, ...]], Any] | None) -> Any:
        if factory is not None:
            return factory(urls)
        try:
            import apprise  # lazy: imported only when notifications are actually enabled
        except Exception as exc:  # pragma: no cover - apprise is a declared dependency
            self._log.error(f"apprise import failed; notifications disabled: {exc}")
            return None
        # Quiet apprise's own logger: at LOG_LEVEL=DEBUG it is chatty and may echo
        # URL detail (it masks credentials, but we don't want host/path either).
        logging.getLogger("apprise").setLevel(logging.WARNING)
        # Brand the notifications so an operator knows what is alerting them.
        asset = apprise.AppriseAsset(
            app_id="gluetun-monitor",
            app_desc="gluetun-monitor",
            app_url="https://github.com/csmarshall/gluetun-monitor",
        )
        ap = apprise.Apprise(asset=asset)
        for url in urls:
            # Never log the URL itself — it carries tokens.
            if not ap.add(url):
                self._log.warn("An APPRISE_URLS entry was rejected by apprise (check its scheme)")
        return ap

    def notify(self, event: NotifyEvent) -> None:
        """Send a single event (a batch of one)."""
        self.notify_batch([event])

    def notify_batch(self, events: list[NotifyEvent]) -> None:
        """Filter ``events`` by the tier floor, then emit one rollup. Never raises."""
        try:
            if self._apprise is None or not events:
                return
            survivors = [e for e in events if tier_rank(e.tier) <= self._level]
            for e in events:
                if tier_rank(e.tier) > self._level:
                    self._log.debug(f"notify: {e.key} (tier={e.tier}) below level — not sent")
            if not survivors:
                return
            title, body, ntype = self._compose(survivors)
            if self._dispatch(lambda: self._apprise.notify(title=title, body=body, notify_type=ntype)):
                self._log.debug(f"notify: pushed {len(survivors)} event(s) as 1 digest")
            else:
                self._log.debug("notify digest reported no success")
        except Exception as exc:  # Tenet 7: a notify failure never touches the loop
            self._log.debug(f"notify error (swallowed): {exc}")

    def test(self) -> bool:
        """Send a test notification (bypasses the tier filter); True if delivered."""
        if self._apprise is None:
            return False
        return self._dispatch(lambda: self._apprise.notify(
            title="gluetun-monitor test notification",
            body="If you can read this, gluetun-monitor notifications are working.",
            notify_type="info",
        ))

    def _compose(self, events: list[NotifyEvent]) -> tuple[str, str, str]:
        """Render survivors into one (title, body, notify_type). Worst tier wins the type."""
        ordered = sorted(events, key=lambda e: tier_rank(e.tier))  # action first
        ntype = _TIER_NOTIFY_TYPE.get(ordered[0].tier, "info")
        if len(ordered) == 1:
            return ordered[0].title, ordered[0].body, ntype
        title = f"gluetun-monitor — {len(ordered)} events this cycle"
        body = "\n".join(f"• [{e.tier}] {e.title}: {e.body}" for e in ordered)
        return title, body, ntype

    def _dispatch(self, send: Callable[[], Any]) -> bool:
        """Run ``send`` on a daemon thread and wait at most ``timeout_seconds``.

        Apprise's ``notify`` is synchronous, so calling it inline would let a slow
        or hung endpoint add latency to the monitoring loop. Bounding it keeps the
        send **off the hot path with a timeout** (Tenet 7 / #22): if it overruns we
        stop waiting and carry on — the daemon thread is harmless and any error in
        it is swallowed.
        """
        result: list[bool] = []

        def run() -> None:
            try:
                result.append(bool(send()))
            except Exception as exc:
                self._log.debug(f"notify error (swallowed): {exc}")
                result.append(False)

        # If the previous send is still hung, don't spawn another — that's the
        # unbounded thread leak (#85). Drop this one (the alert lifecycle re-reports
        # an ongoing problem anyway, so nothing is permanently lost).
        if self._inflight is not None and self._inflight.is_alive():
            self._log.debug("notify: a previous send is still in-flight; dropping this one")
            return False

        worker = threading.Thread(target=run, daemon=True)
        self._inflight = worker
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            self._log.debug(f"notify exceeded {self._timeout}s; not blocking the loop")
            return False
        self._inflight = None
        return result[0] if result else False


def build_notifier(config: Config, logger: Logger) -> Notifier:
    """A :class:`NullNotifier` when ``APPRISE_URLS`` is unset; an
    :class:`AppriseNotifier` otherwise.
    """
    if not config.apprise_urls:
        return NullNotifier()
    return AppriseNotifier(
        config.apprise_urls,
        level=config.notify_level,
        timeout_seconds=config.notify_timeout,
        logger=logger,
    )
