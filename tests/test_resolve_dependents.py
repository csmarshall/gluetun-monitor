"""_resolve_dependents reuses existence it already learned this loop.

Why: it inspects the current names to check existence, then prunes the remembered
set. Re-inspecting a name it just checked is wasted Docker API work; this pins
that a name that is both current and remembered is inspected once per loop, not
twice — while a remembered-but-no-longer-current name is still inspected (and
pruned if gone).
"""

from __future__ import annotations

import io
from collections import Counter

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ContainerInfo
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


class _CountingClient(FakeDockerClient):
    """Counts inspect() calls per name."""

    def __init__(self) -> None:
        super().__init__()
        self.inspect_calls: Counter[str] = Counter()

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        self.inspect_calls[name_or_id] += 1
        return super().inspect(name_or_id)


def _mon(fake: FakeDockerClient, **cfg: object) -> Monitor:
    cfg.setdefault("config_file", "/dev/null")
    cfg.setdefault("gluetun_container", "gluetun")
    logger = Logger(log_file=None, level="INFO", stream=io.StringIO())
    return Monitor(fake, Config(**cfg), logger, sleep=lambda _s: None, stats=SiteStatsStore(None))


def test_current_dependent_inspected_once_per_loop() -> None:
    fake = _CountingClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon = _mon(fake, dependent_containers="dep")  # manual -> no discovery inspects
    result = mon._resolve_dependents()
    assert result == ["dep"]
    assert fake.inspect_calls["dep"] == 1  # not re-inspected during the prune


def test_remembered_but_gone_dependent_is_pruned() -> None:
    """A remembered name not in the current set is still inspected and, if gone,
    dropped — the prune correctness the inspect-reuse must not break."""
    fake = _CountingClient()
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon = _mon(fake, dependent_containers="dep")
    assert mon._resolve_dependents() == ["dep"]  # dep now remembered

    fake.remove("dep", volumes=False)  # disappears
    assert mon._resolve_dependents() == []  # pruned from the remembered set


def test_remembered_dependent_reconfigured_off_gluetun_is_pruned() -> None:
    """A remembered name whose container was recreated under plain bridge
    networking (compose dropped ``network_mode: container:gluetun`` entirely) is
    no longer a dependent, even though it still exists under the same name.

    Existence alone can't distinguish this from the legitimate stale-id-stranded
    case the remembered set exists for (ADR-0014) — only the current NetworkMode
    can. Without this check the name would be phantom-managed forever, since
    nothing else ever removes a name from ``known_dependents`` except the
    container disappearing outright. Auto-discovery (the real deployment's mode)
    is what actually exercises this: a manual ``DEPENDENT_CONTAINERS`` list is
    taken at face value regardless of NetworkMode, by design.
    """
    fake = _CountingClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    mon = _mon(fake)  # default dependent_containers="auto"
    assert mon._resolve_dependents() == ["dep"]  # dep now remembered

    # Simulate a real recreate: old container gone, new one under the same name
    # with unrelated (non-container-mode) networking. Auto-discovery no longer
    # finds it (NetworkMode doesn't match gluetun), so this only reaches the
    # remembered-set prune path being tested here.
    fake.remove("dep", volumes=False)
    fake.add_container("dep", network_mode="bridge")
    assert mon._resolve_dependents() == []  # no longer a dependent, though it exists
