"""Durable dependent memory: gluetun id history + known dependent names (#97).

The monitor's knowledge of its dependents lives in two places, and both fail at
once when a monitor restart is followed closely by a gluetun recreate:
auto-discovery matches ``container:<CURRENT gluetun id>`` among *running*
containers (a stranded dependent points at the old id, and may be exited), and
the in-memory remembered set (ADR-0004) starts empty. Result: stranded
dependents become permanently invisible to remediation — observed live, twice
in one evening, in both forms (running-stranded and exited-stranded).

This module persists ONLY what Docker forgets:

* ``gluetun_ids`` — every container id gluetun has been seen under. Docker ids
  are 256-bit random and never reused, so an entry can never rot into a false
  positive: any container whose ``NetworkMode`` references a dead id from this
  list provably shared OUR gluetun's netns → it is safe to adopt and heal.
  Pruned **by reference**, not by age or count: an id is dropped only when no
  existing container references it (a fixed "last N" could evict an id a
  long-exited dependent still points at). No freshness gating, no host boot-id
  — a reboot merely converts running-stranded into exited-stranded, which is
  already the adoption case.
* ``known_dependents`` — the remembered set, so a monitor restart no longer
  wipes it. Pruned to still-existing containers by the caller (same rule the
  in-memory set already uses).

Everything else (which container references which id, run state, mounts) is
read fresh from the daemon each loop — Docker's records are the authoritative
join, and duplicating them here would only create a second source of truth.

``schema_version`` and ``updated_at`` are observability only, never decision
inputs. Persistence follows the site-stats contract: atomic tmp+rename+fsync
writes, and a corrupt/missing file degrades to empty memory (today's behavior)
— state must never take monitoring down (#73 principle).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

_SCHEMA_VERSION = 1


class MonitorState:
    """Load/hold/persist the dependent memory. Write-on-change only."""

    def __init__(self, path: str | None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path) if path else None
        self._clock = clock
        self.gluetun_ids: list[str] = []  # newest first
        self.known_dependents: set[str] = set()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            ids = data.get("gluetun_ids", [])
            deps = data.get("known_dependents", [])
            self.gluetun_ids = [str(i) for i in ids if isinstance(i, str) and i]
            self.known_dependents = {str(d) for d in deps if isinstance(d, str) and d}
        except FileNotFoundError:
            pass  # first run — empty memory is the documented bootstrap gap
        except (OSError, ValueError, TypeError, AttributeError):
            # Corrupt/garbage state is non-fatal: degrade to empty memory (the
            # pre-#97 behavior) rather than take the watchdog down over it.
            self.gluetun_ids = []
            self.known_dependents = set()

    # ----- mutation (all write-on-change) -----

    def record_gluetun_id(self, container_id: str) -> None:
        """Remember ``container_id`` as gluetun's current id (newest first)."""
        if not container_id:
            return
        if self.gluetun_ids[:1] == [container_id]:
            return
        if container_id in self.gluetun_ids:
            self.gluetun_ids.remove(container_id)
        self.gluetun_ids.insert(0, container_id)
        self._dirty = True

    def set_known_dependents(self, names: set[str]) -> None:
        """Replace the persisted remembered set (caller has already pruned it)."""
        if names != self.known_dependents:
            self.known_dependents = set(names)
            self._dirty = True

    def prune_gluetun_ids(self, referenced: set[str]) -> None:
        """Drop ids no existing container references (reference-based pruning).

        ``referenced`` must include the CURRENT gluetun id and every id that any
        existing container's ``NetworkMode`` points at — an id stays exactly as
        long as it can still matter to an adoption decision.
        """
        kept = [i for i in self.gluetun_ids if i in referenced]
        if kept != self.gluetun_ids:
            self.gluetun_ids = kept
            self._dirty = True

    def is_former_gluetun(self, container_id: str) -> bool:
        """True if ``container_id`` is a (current or former) gluetun id."""
        return container_id in self.gluetun_ids

    # ----- persistence -----

    def save(self) -> bool:
        """Persist if dirty. Atomic + fsync'd (site-stats pattern); best-effort —
        an I/O failure leaves the previous complete file and never blocks the loop.
        """
        if self._path is None or not self._dirty:
            return False
        payload = {
            # Observability only — never decision inputs.
            "schema_version": _SCHEMA_VERSION,
            "updated_at": self._clock(),
            "gluetun_ids": self.gluetun_ids,
            "known_dependents": sorted(self.known_dependents),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._path)
            self._fsync_dir(self._path.parent)
            self._dirty = False
            return True
        except OSError:
            return False

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort fsync of a directory so a rename survives power loss."""
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
