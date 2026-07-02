"""The recreate critical section is no longer destructive (#76).

Why: remove-then-create had a window where a stop signal or Docker API failure
permanently destroyed the dependent (the create body existed only in memory).
The rename-aside sequence (park → create → remove old → start) makes every
intermediate state recoverable: create failures roll back, stop signals are
deferred until the containers are consistent, and a SIGKILL/power-loss leftover
is healed by the startup sweep. These tests pin each of those guarantees.
"""

from __future__ import annotations

import io
import os
import signal
from typing import Any

from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recreate import (
    gc_recreate_leftover,
    recreate_dependent,
    sweep_recreate_leftovers,
)

from .fakes import FakeDockerClient

OLD_ID = "b" * 64
GLUETUN_ID = "a" * 64


def _logger() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def _stranded_dep(fake: FakeDockerClient, name: str = "dep") -> None:
    fake.add_container(name, network_mode=f"container:{OLD_ID}")


# ----- signal deferral: SIGTERM mid-recreate must not strand the sequence -----

def test_sigterm_mid_recreate_is_deferred_until_consistent() -> None:
    """A SIGTERM landing between park and create (the compose-restart trigger)
    is held until the sequence completes, then re-delivered — the dependent
    exists and is started BEFORE the shutdown handler runs."""
    received: list[int] = []  # started-count observed when the signal arrives

    class _KillDuringCreate(FakeDockerClient):
        def create_from_config(self, config: dict[str, Any], name: str) -> str:
            os.kill(os.getpid(), signal.SIGTERM)  # lands mid-critical-section
            return super().create_from_config(config, name)

    fake = _KillDuringCreate()
    _stranded_dep(fake)

    def _record(signum: int, _frame: object) -> None:
        received.append(len(fake.started))  # how far the recreate got by delivery

    original = signal.signal(signal.SIGTERM, _record)
    try:
        assert recreate_dependent(fake, "dep", GLUETUN_ID, _logger()) is True
    finally:
        signal.signal(signal.SIGTERM, original)  # never leak handlers (see #90)

    # The signal WAS re-delivered — but only after the replacement was started.
    assert received == [1]
    info = fake.inspect("dep")
    assert info is not None and info.running
    assert info.network_mode == f"container:{GLUETUN_ID}"


# ----- API failures at each step leave a recoverable state -----

def test_rename_failure_changes_nothing() -> None:
    class _RenameRaises(FakeDockerClient):
        def rename(self, name_or_id: str, new_name: str) -> None:
            raise RuntimeError("socket died")

    fake = _RenameRaises()
    _stranded_dep(fake)
    assert recreate_dependent(fake, "dep", GLUETUN_ID, _logger()) is False
    assert fake.removed == [] and fake.created == [] and fake.started == []
    assert fake.inspect("dep") is not None  # untouched


def test_remove_old_failure_does_not_start_the_replacement() -> None:
    """If the parked original can't be removed, the replacement must NOT start:
    both containers share every mount, and two live instances would corrupt
    the volumes. The next loop retries (restart path GCs the leftover first)."""
    class _RemoveRaises(FakeDockerClient):
        def remove(self, name_or_id: str, *, volumes: bool) -> None:
            raise RuntimeError("socket died")

    fake = _RemoveRaises()
    _stranded_dep(fake)
    assert recreate_dependent(fake, "dep", GLUETUN_ID, _logger()) is False
    assert fake.started == []  # no double-mount
    # Replacement exists under the real name; original is parked — both alive,
    # neither started: the exact state gc_recreate_leftover + restart heal.
    assert fake.inspect("dep") is not None
    assert fake.inspect("dep.gm-recreate-old") is not None


# ----- leftover GC before any remediation -----

def test_gc_removes_leftover_and_blocks_when_it_cannot() -> None:
    fake = FakeDockerClient()
    _stranded_dep(fake)
    fake.add_container("dep.gm-recreate-old", network_mode=f"container:{OLD_ID}")
    assert gc_recreate_leftover(fake, "dep", _logger()) is None
    assert ("dep.gm-recreate-old", False) in fake.removed

    class _RemoveRaises(FakeDockerClient):
        def remove(self, name_or_id: str, *, volumes: bool) -> None:
            raise RuntimeError("socket died")

    blocked = _RemoveRaises()
    _stranded_dep(blocked)
    blocked.add_container("dep.gm-recreate-old", network_mode=f"container:{OLD_ID}")
    assert gc_recreate_leftover(blocked, "dep", _logger()) is not None  # caller must not act


def test_gc_noop_without_leftover() -> None:
    fake = FakeDockerClient()
    _stranded_dep(fake)
    assert gc_recreate_leftover(fake, "dep", _logger()) is None
    assert fake.removed == []


# ----- startup sweep: SIGKILL/power-loss recovery -----

def test_sweep_restores_parked_container_when_name_is_free() -> None:
    """Killed between park and create: the parked container IS the workload —
    rename it back so discovery and remediation see it again."""
    fake = FakeDockerClient()
    fake.add_container("app.gm-recreate-old", network_mode=f"container:{OLD_ID}")
    sweep_recreate_leftovers(fake, _logger())
    assert fake.inspect("app") is not None
    assert fake.inspect("app.gm-recreate-old") is None
    assert ("app.gm-recreate-old", "app") in [(o, n) for o, n in fake.renamed]


def test_sweep_removes_parked_container_when_replaced() -> None:
    """Killed after create: the replacement owns the name (and the volumes) —
    the parked original is condemned."""
    fake = FakeDockerClient()
    fake.add_container("app", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("app.gm-recreate-old", network_mode=f"container:{OLD_ID}")
    sweep_recreate_leftovers(fake, _logger())
    assert ("app.gm-recreate-old", False) in fake.removed
    assert fake.inspect("app") is not None  # replacement untouched


def test_sweep_ignores_unrelated_containers() -> None:
    fake = FakeDockerClient()
    fake.add_container("app", network_mode=f"container:{GLUETUN_ID}")
    sweep_recreate_leftovers(fake, _logger())
    assert fake.removed == [] and fake.renamed == []
