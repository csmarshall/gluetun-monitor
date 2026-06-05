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
* **silent removal** for a problem whose subject was *forgotten* (site removed from
  config, dependent excluded/gone) — a "recovered" there would be a lie.

State persists to a JSON sidecar, and the loop counter persists with it, so a restart
of the monitor neither re-spams still-broken problems nor misses a resolve that
happened while it was down.
"""

from __future__ import annotations

import json
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
        self._load()

    # ----- per-loop input from the monitor -----

    def begin_loop(self) -> None:
        """Open a loop: advance the (persistent) counter, clear this loop's inputs."""
        self._loop += 1
        self._reported = {}
        self._forgotten = set()

    def report(self, key: str, tier: str, title: str, body: str) -> None:
        """Declare that problem ``key`` is currently true this loop."""
        self._reported[key] = (tier, title, body)

    def forget(self, key: str) -> None:
        """Declare that ``key``'s subject is no longer monitored (silent clear)."""
        self._forgotten.add(key)

    # ----- per-loop output -----

    def events(self) -> list[NotifyEvent]:
        """Diff this loop's reported problems against the persisted active set."""
        out: list[NotifyEvent] = []

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
            active = self._active.pop(key)
            if key in self._forgotten:
                self._log.debug(f"notify: {key} subject removed — silent clear")
                continue
            active_for = self._loop - active.since_loop
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
        except (OSError, ValueError, KeyError, TypeError):
            # Corrupt/missing sidecar: start clean rather than crash the watchdog.
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
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f"{self._path}.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            tmp.replace(self._path)
        except OSError as exc:
            self._log.debug(f"notify: could not persist alert state: {exc}")
