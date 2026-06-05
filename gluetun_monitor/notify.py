"""Opt-in external notification layer (issue #22, ADR-0010).

By default (``APPRISE_URLS`` unset) this is a :class:`NullNotifier`: zero behavior
change — the monitor stays a log-only tool. When configured, significant and
infrequent events — gluetun restart, recovery failure, dependent remediation, the
flaky-site advisory, and refusal to start — are pushed out-of-band via
`Apprise <https://github.com/caronc/apprise>`_ (100+ backends, configured by URL).

Tenet 7 (best-effort, non-blocking): a notification failure must **never** affect
monitoring. Every send is wrapped and swallowed (logged at DEBUG), filtered by a
minimum severity, and throttled per event key so a persistent fault can't spam.
Apprise URLs carry secrets, so they are never logged.

The library is exercised for real in CI (a localhost webhook sink), which is what
lets Dependabot auto-merge apprise patch/minor — the same bar docker-py clears via
the real-daemon test (#24).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .config import Config
    from .logging_setup import Logger

# Severity aligned with stdlib logging so NOTIFY_MIN_LEVEL reuses the log names.
_LEVEL_BY_NAME: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

# Our level names → Apprise notify-type strings (apprise's NotifyType is str-based,
# so the value is accepted directly; the real-apprise CI test confirms this).
_APPRISE_TYPE_BY_LEVEL = {"INFO": "info", "WARN": "warning", "WARNING": "warning", "ERROR": "failure"}


def level_value(name: str) -> int:
    """Map a level name (INFO/WARN/ERROR) to its numeric severity (default WARN)."""
    return _LEVEL_BY_NAME.get(name.upper(), logging.WARNING)


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """One notifiable occurrence.

    ``key`` is the throttle/dedup key: at most one send per key per throttle window
    (mirrors the in-log dedup so a persistent fault notifies once, not every loop).
    """

    level: str  # "INFO" | "WARN" | "ERROR"
    title: str
    body: str
    key: str


class Notifier(Protocol):
    """Sink for :class:`NotifyEvent`. Implementations must never raise."""

    def notify(self, event: NotifyEvent) -> None:
        """Best-effort send of one event."""
        ...

    def test(self) -> bool:
        """Send a test notification; return True if it was delivered."""
        ...


class NullNotifier:
    """The default when ``APPRISE_URLS`` is unset — notifications disabled."""

    def notify(self, event: NotifyEvent) -> None:
        """No-op."""
        return

    def test(self) -> bool:
        """Nothing configured to test."""
        return False


class AppriseNotifier:
    """:class:`Notifier` over Apprise: min-level filter + per-key throttle, fully
    swallowed.

    ``apprise_factory`` is injectable so the wrapper logic (filter/throttle/swallow)
    is unit-testable with no library and no network; production passes ``None`` and
    the real ``apprise`` is imported lazily (only when notifications are enabled).
    """

    def __init__(
        self,
        urls: tuple[str, ...],
        *,
        min_level: str,
        throttle_seconds: int,
        logger: Logger,
        timeout_seconds: int = 10,
        now: Callable[[], float] = monotonic,
        apprise_factory: Callable[[tuple[str, ...]], Any] | None = None,
    ) -> None:
        self._min = level_value(min_level)
        self._throttle = throttle_seconds
        self._timeout = timeout_seconds
        self._log = logger
        self._now = now
        self._last_sent: dict[str, float] = {}
        self._apprise = self._build(urls, apprise_factory)

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
        """Send ``event`` if it clears the min level and isn't throttled. Never raises."""
        try:
            if self._apprise is None:
                return
            if level_value(event.level) < self._min:
                return
            if self._throttled(event.key):
                self._log.debug(f"notify throttled: {event.key}")
                return
            ntype = _APPRISE_TYPE_BY_LEVEL.get(event.level.upper(), "warning")
            if self._dispatch(lambda: self._apprise.notify(
                title=event.title, body=event.body, notify_type=ntype
            )):
                self._last_sent[event.key] = self._now()
            else:
                self._log.debug(f"notify send reported no success: {event.key}")
        except Exception as exc:  # Tenet 7: a notify failure never touches the loop
            self._log.debug(f"notify error (swallowed): {exc}")

    def test(self) -> bool:
        """Send a test notification (bypasses level/throttle); True if delivered."""
        if self._apprise is None:
            return False
        return self._dispatch(lambda: self._apprise.notify(
            title="gluetun-monitor test notification",
            body="If you can read this, gluetun-monitor notifications are working.",
            notify_type="info",
        ))

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

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            self._log.debug(f"notify exceeded {self._timeout}s; not blocking the loop")
            return False
        return result[0] if result else False

    def _throttled(self, key: str) -> bool:
        if self._throttle <= 0:
            return False
        last = self._last_sent.get(key)
        return last is not None and (self._now() - last) < self._throttle


def build_notifier(config: Config, logger: Logger) -> Notifier:
    """A :class:`NullNotifier` when ``APPRISE_URLS`` is unset; an
    :class:`AppriseNotifier` otherwise.
    """
    if not config.apprise_urls:
        return NullNotifier()
    return AppriseNotifier(
        config.apprise_urls,
        min_level=config.notify_min_level,
        throttle_seconds=config.notify_throttle,
        timeout_seconds=config.notify_timeout,
        logger=logger,
    )
