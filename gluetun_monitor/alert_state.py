"""Active-alert lifecycle with best-effort persistence (ADR-0012).

The notification layer (ADR-0010/0011) is **edge-triggered**: an ongoing problem is
announced when it *starts*, not every loop it persists. This component owns that
state machine so the notifier itself stays a dumb sink.

Each loop the monitor reports the problems currently true via :meth:`report`, and the
subjects it has stopped managing via :meth:`forget`. At loop end :meth:`events` yields:

* a **new** alert for each problem that just appeared,
* a **reminder** for an ongoing problem every ``repeat_interval`` loops (0 = never —
  announce once, then stay silent until it resolves),
* a **resolve** (same tier as the alert) for a problem that **cleared** — its subject
  still exists, so "it's back" is true, and
* a **deprecation** notice for a problem whose subject was *forgotten* (site removed
  from config, dependent excluded/gone) — "no longer monitored", since a "recovered"
  there would be a lie.

A loop that could not evaluate every subject — it died on an exception, or bailed
early (gluetun down, recovery failed) before reaching the dependent phase — must
call :meth:`mark_incomplete`: on such a loop, silence is *absence of evidence*, not
recovery, so unreported active alerts are held instead of resolved (#74). Problems
that WERE reported (or explicitly forgotten) before the abort still process
normally — they were evaluated.

State persists to a JSON sidecar, and the loop counter persists with it, so a restart
of the monitor neither re-spams still-broken problems nor misses a resolve that
happened while it was down.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .notify import NotifyEvent

if TYPE_CHECKING:
    from .logging_setup import Logger


@dataclass
class _Active:
    """One currently-firing problem alert."""

    tier: str
    title: str
    body: str
    since_loop: int
    last_notified_loop: int


class AlertState:
    """Edge-triggered active-alert lifecycle for the notification layer."""

    def __init__(self, path: str | None, *, logger: Logger, repeat_interval: int) -> None:
        self._path = path
        self._log = logger
        self._repeat = repeat_interval
        self._loop = 0
        self._active: dict[str, _Active] = {}
        self._reported: dict[str, tuple[str, str, str]] = {}  # this loop: key -> (tier,title,body)
        self._forgotten: set[str] = set()  # this loop: subjects no longer monitored
        self._incomplete = False  # this loop: aborted before evaluating everything
        self._load()

    # ----- per-loop input from the monitor -----

    def begin_loop(self) -> None:
        """Open a loop: advance the (persistent) counter, clear this loop's inputs."""
        self._loop += 1
        self._reported = {}
        self._forgotten = set()
        self._incomplete = False

    def mark_incomplete(self) -> None:
        """Declare that this loop did NOT evaluate every subject (it aborted on an
        exception or bailed early). :meth:`events` will then hold unreported active
        alerts instead of resolving them — an unevaluated problem didn't clear, we
        just never looked (#74).
        """
        self._incomplete = True

    def report(self, key: str, tier: str, title: str, body: str) -> None:
        """Declare that problem ``key`` is currently true this loop."""
        self._reported[key] = (tier, title, body)

    def forget(self, key: str) -> None:
        """Declare that ``key``'s subject is no longer monitored (silent clear)."""
        self._forgotten.add(key)

    def hold(self, key: str) -> None:
        """Re-assert an already-active alert *as-is* so this loop doesn't resolve it.

        For a subject the loop couldn't evaluate (e.g. a dependent that came back
        un-probeable): the problem hasn't cleared, we just didn't get to check it, so
        the alert must stay active rather than false-resolve. No-op if ``key`` isn't
        active. Cheaper/more precise than ``mark_incomplete`` when only one subject
        is unevaluated — it holds that alert, not every active alert.
        """
        active = self._active.get(key)
        if active is not None:
            self._reported[key] = (active.tier, active.title, active.body)

    def is_active(self, key: str) -> bool:
        """Whether ``key`` is currently an active (announced, unresolved) alert.

        Lets the monitor recognize a problem it had already escalated before a
        restart (#98) without persisting separate bookkeeping — the alert sidecar
        already remembers.
        """
        return key in self._active

    def supersede(self, key: str) -> None:
        """Silently retire ``key``: a stronger alert for the same subject replaces
        it (#98 unhealthy→wedged escalation). No resolve event (the problem did
        not clear) and no deprecation (the subject is still monitored) — the
        successor alert is the continuation of the same story.
        """
        if self._active.pop(key, None) is not None:
            self._log.debug(f"notify: {key} superseded")
        self._reported.pop(key, None)

    # ----- per-loop output -----

    def events(self) -> list[NotifyEvent]:
        """Diff this loop's reported problems against the persisted active set."""
        out: list[NotifyEvent] = []

        # A forget wins over a same-loop report: if a subject was declared no-longer-
        # monitored, retire it even if another path re-reported it this loop (e.g. a
        # removed-but-still-dominant flaky site whose restarts haven't aged out).
        for key in self._forgotten:
            self._reported.pop(key, None)

        for key, (tier, title, body) in self._reported.items():
            active = self._active.get(key)
            if active is None:
                self._active[key] = _Active(tier, title, body, self._loop, self._loop)
                self._log.debug(f"notify: {key} new ({tier})")
                out.append(NotifyEvent(tier=tier, title=title, body=body, key=key))
                continue
            # Refresh the latest summary in case the numbers moved.
            active.tier, active.title, active.body = tier, title, body
            if self._repeat > 0 and (self._loop - active.last_notified_loop) >= self._repeat:
                active.last_notified_loop = self._loop
                active_for = self._loop - active.since_loop
                self._log.debug(f"notify: {key} reminder (active {active_for} loops)")
                out.append(NotifyEvent(
                    tier=tier, title=f"still active: {title}", body=body, key=key,
                ))
            else:
                self._log.debug(f"notify: {key} still active, suppressed (repeat={self._repeat})")

        for key in list(self._active):
            if key in self._reported:
                continue
            if self._incomplete and key not in self._forgotten:
                # The loop never got as far as evaluating this subject: hold the
                # alert. A forget is an explicit statement and still processes.
                self._log.debug(f"notify: {key} held (loop incomplete — not evaluated)")
                continue
            active = self._active.pop(key)
            active_for = self._loop - active.since_loop
            if key in self._forgotten:
                # Subject removed (site dropped / dependent excluded): the alert is
                # retired, not recovered — say so plainly rather than imply a fix.
                self._log.debug(f"notify: {key} subject removed — deprecating alert")
                out.append(NotifyEvent(
                    tier=active.tier,
                    title=f"no longer monitored: {active.title}",
                    body=f"{active.title} — its subject was removed or excluded; "
                    f"this alert is retired (it did not necessarily recover).",
                    key=f"deprecated:{key}",
                ))
                continue
            self._log.debug(f"notify: {key} resolved after {active_for} loops")
            out.append(NotifyEvent(
                tier=active.tier,
                title=f"resolved: {active.title}",
                body=f"{active.title} has cleared (was active {active_for} loop(s)).",
                key=f"resolve:{key}",
            ))
        return out

    def active_count(self) -> int:
        """How many problems are currently firing (for the startup/heartbeat log)."""
        return len(self._active)

    # ----- persistence (best-effort, like the stats sidecar) -----

    def _load(self) -> None:
        if not self._path:
            return
        try:
            with Path(self._path).open(encoding="utf-8") as fh:
                raw = json.load(fh)
            self._loop = int(raw.get("loop", 0))
            for key, rec in (raw.get("active") or {}).items():
                self._active[key] = _Active(
                    tier=str(rec["tier"]),
                    title=str(rec["title"]),
                    body=str(rec["body"]),
                    since_loop=int(rec["since_loop"]),
                    last_notified_loop=int(rec["last_notified_loop"]),
                )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Corrupt/missing sidecar: start clean rather than crash the watchdog.
            # AttributeError covers a well-formed-JSON-but-not-an-object sidecar
            # (null / [] / 5 / "str"), whose .get()/.items() would otherwise escape
            # this handler and take the monitor down at startup — violating the
            # tenet that notifications must never affect monitoring.
            self._loop = 0
            self._active = {}

    def save(self) -> None:
        """Persist the active set + loop counter atomically. Best-effort."""
        if not self._path:
            return
        payload = {
            "loop": self._loop,
            "active": {key: asdict(a) for key, a in self._active.items()},
        }
        try:
            target = Path(self._path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f"{self._path}.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())  # data on disk before the rename
            tmp.replace(self._path)  # atomic rename
            self._fsync_dir(target.parent)  # persist the rename itself (mirror SiteStatsStore)
        except OSError as exc:
            self._log.debug(f"notify: could not persist alert state: {exc}")

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort fsync of a directory so the rename survives power loss."""
        try:
            fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
