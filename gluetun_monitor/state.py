"""In-memory state: consecutive-failure counters and state-machine enums.

Counters are consecutive and reset on any pass; nothing is persisted and there
is no backoff/circuit-breaker (ADR-0006, Tenet 9 / ROC): recovery is cheap and
non-destructive, so we re-evaluate every loop rather than carry fragile state.
A monitor restart zeroes everything.
"""

from __future__ import annotations

from enum import Enum, auto


class InterfaceStatus(Enum):
    """Result of the ADR-0004 interface check (``ls /sys/class/net``)."""

    LIVE = auto()  # has a non-loopback interface (eth0/tun0) — eligible worker
    STRANDED = auto()  # only ``lo`` — netns binding lost; hard failure
    UNKNOWN = auto()  # could not determine (e.g. distroless, exec failed)


class RemediationAction(Enum):
    """The ADR-0004 restart-vs-recreate decision for a failing dependent."""

    RESTART = auto()  # NetworkMode target == current gluetun id -> docker restart
    RECREATE = auto()  # target != current id -> must recreate (NetworkMode immutable)
    TRY_RESTART = auto()  # name-form / unreadable target -> try restart, escalate


class Counter:
    """Per-key consecutive-failure counter. ``fail`` bumps, ``reset`` zeroes."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def fail(self, key: str) -> int:
        """Increment ``key``'s consecutive-failure count and return the new value."""
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def reset(self, key: str) -> None:
        """Zero ``key``'s counter (a passing loop)."""
        self._counts[key] = 0

    def get(self, key: str) -> int:
        """Current consecutive-failure count for ``key`` (0 if never seen)."""
        return self._counts.get(key, 0)

    def reset_all(self) -> None:
        """Zero every counter (post-recovery, per v1.x ``handle_failure``)."""
        for key in self._counts:
            self._counts[key] = 0
