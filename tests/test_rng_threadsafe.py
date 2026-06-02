"""The load-bearing site shuffle stays sound when dependents are probed in a pool.

Why: _probe_dependent runs across a ThreadPoolExecutor, and a single
random.Random instance is not thread-safe — concurrent sample()/uniform() calls
can interleave and corrupt generator state, silently biasing the shuffle that
ADR-0006 leans on. The monitor guards every draw with a lock; this pins that the
draws are actually serialized (never overlap) under concurrent probing.
"""

from __future__ import annotations

import io
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64
POOL = [f"https://s{i}.example" for i in range(8)]


class _OverlapDetectingRandom(random.Random):
    """Records the peak number of threads simultaneously inside a draw.

    The tiny busy-window widens any unguarded overlap so it is observable; if the
    monitor's lock works, only one thread is ever inside sample()/uniform() at a
    time and max_concurrent stays 1.
    """

    def __init__(self) -> None:
        super().__init__(0)
        self._active = 0
        self.max_concurrent = 0
        self._barrier = threading.Barrier(8)

    def _enter(self) -> None:
        # Synchronize all workers to the same instant so an unguarded RNG would
        # genuinely overlap, then observe how many are concurrently inside.
        # When the lock works, only one thread is ever here, so the 8-party
        # barrier never fills and the first draw times out (cheaply); when it is
        # broken, all 8 arrive together and the barrier trips instantly.
        try:
            self._barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        time.sleep(0.01)  # real yield: widens the window so any overlap is observed
        self._active -= 1

    def sample(self, population, k, *, counts=None):  # type: ignore[override]
        self._enter()
        return super().sample(population, k, counts=counts)


def _live_exec(name: str, cmd: list[str]) -> ExecResult:
    if cmd[:2] == ["ls", "/sys/class/net"]:
        return ExecResult(0, "eth0\nlo\n")  # LIVE
    return ExecResult(0, "  HTTP/1.1 200 OK\n")


def test_rng_draws_are_serialized_under_concurrent_probing() -> None:
    fake = FakeDockerClient()
    fake.on_exec = _live_exec
    rng = _OverlapDetectingRandom()
    logger = Logger(log_file=None, level="INFO", stream=io.StringIO())
    mon = Monitor(
        fake,
        Config(config_file="/dev/null", gluetun_container="gluetun"),
        logger,
        rng=rng,
        sleep=lambda _s: None,
        stats=SiteStatsStore(None),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(mon._probe_dependent, f"dep{i}", GLUETUN_ID, POOL, [])
            for i in range(8)
        ]
        for f in futures:
            f.result()

    assert rng.max_concurrent == 1, (
        f"RNG draws overlapped (peak {rng.max_concurrent}) — the lock did not "
        f"serialize them; the shuffle is not thread-safe"
    )
