"""Real-daemon integration test (issue #24).

The unit suite runs against ``FakeDockerClient`` — it proves our *logic*, not that
docker-py still behaves the way we assume. This module drives the real
``DockerPyClient`` against a live ``dockerd`` (as CI provides), exercising every
docker-py path the monitor depends on: ``ping`` / ``list_running_ids`` / ``inspect``
/ ``exec_run`` / ``logs`` / ``restart``, and the non-destructive recreate
(``remove`` without volumes → ``create_from_config`` → ``start``).

The marquee assertion is ADR-0005's data-preservation guarantee: a dependent's
named volume — and the data in it — survives the recreate. A docker-py regression
in any of these paths turns this red, which is the bar issue #24 sets before
docker-py minors may be auto-merged.

Marked ``integration``; the unit job deselects it (``-m "not integration"``). Run it
against a daemon with ``pytest -m integration``. It self-skips if none is reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from gluetun_monitor.docker_client import DockerPyClient
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recreate import recreate_dependent

# docker-py is a runtime dependency, so import (not skip) — but a missing daemon
# skips at the fixture level below.
docker = pytest.importorskip("docker")

pytestmark = pytest.mark.integration

# Tiny, ubiquitous image: alpine provides sh/cat for the exec + volume checks.
IMAGE = "alpine:3"
SENTINEL = "gluetun-monitor-survives-recreate"


@pytest.fixture(scope="module")
def client() -> DockerPyClient:
    """A real ``DockerPyClient``, or skip the whole module if no daemon answers."""
    try:
        c = DockerPyClient(timeout=30)
        if not c.ping():
            pytest.skip("no reachable Docker daemon")
    except Exception as exc:  # docker-py import/connect failure
        pytest.skip(f"Docker unavailable: {exc}")
    return c


@pytest.fixture
def world(client: DockerPyClient) -> Iterator[dict[str, Any]]:
    """Bring up a gluetun-like netns owner + a dependent sharing it, with a named
    volume holding nothing yet. Everything is torn down afterward (force-remove)."""
    raw = docker.from_env()
    suffix = uuid.uuid4().hex[:8]
    gluetun = f"gm-it-gluetun-{suffix}"
    gluetun2 = f"gm-it-gluetun2-{suffix}"
    dep = f"gm-it-dep-{suffix}"
    vol = f"gm-it-vol-{suffix}"

    raw.images.pull(IMAGE)
    raw.volumes.create(vol)
    g1 = raw.containers.run(IMAGE, ["sleep", "3600"], detach=True, name=gluetun)
    raw.containers.run(
        IMAGE,
        ["sleep", "3600"],
        detach=True,
        name=dep,
        network_mode=f"container:{g1.id}",
        volumes={vol: {"bind": "/data", "mode": "rw"}},
    )
    try:
        yield {
            "client": client,
            "raw": raw,
            "gluetun": gluetun,
            "gluetun2": gluetun2,
            "dep": dep,
            "vol": vol,
            "g1_id": g1.id,
        }
    finally:
        for name in (dep, gluetun, gluetun2):
            try:
                raw.containers.get(name).remove(force=True)
            except Exception:
                pass
        try:
            raw.volumes.get(vol).remove(force=True)
        except Exception:
            pass


def test_real_docker_paths_and_volume_survives_recreate(world: dict[str, Any]) -> None:
    """End to end against a real daemon: probe → restart → recreate, asserting the
    volume's data survives the (volumes=False) remove inside the recreate."""
    client: DockerPyClient = world["client"]
    raw = world["raw"]
    dep = world["dep"]
    g1_id = world["g1_id"]

    # ping / list_running_ids
    assert client.ping() is True
    assert g1_id in client.list_running_ids()

    # inspect: the dependent shares gluetun's netns
    info = client.inspect(dep)
    assert info is not None
    assert info.running
    assert info.network_mode == f"container:{g1_id}"
    original_dep_id = info.id

    # exec_run: write then read a sentinel into the named volume
    write = client.exec_run(dep, ["sh", "-c", f"echo {SENTINEL} > /data/sentinel"])
    assert write.exit_code == 0, write.output
    read = client.exec_run(dep, ["cat", "/data/sentinel"])
    assert read.exit_code == 0
    assert SENTINEL in read.output

    # logs: smoke — decodes to text without error
    assert isinstance(client.logs(world["gluetun"], tail=2000), str)

    # restart preserves identity (same container id) and never touches the volume
    client.restart(dep)
    after_restart = client.inspect(dep)
    assert after_restart is not None
    assert after_restart.id == original_dep_id
    assert SENTINEL in client.exec_run(dep, ["cat", "/data/sentinel"]).output

    # --- simulate a gluetun recreate: a NEW netns owner with a new id ---
    g2 = raw.containers.run(IMAGE, ["sleep", "3600"], detach=True, name=world["gluetun2"])

    assert recreate_dependent(client, dep, g2.id, Logger(level="DEBUG")) is True

    # the dependent is back, re-homed onto the new gluetun, and is a NEW container
    rehomed = client.inspect(dep)
    assert rehomed is not None
    assert rehomed.running
    assert rehomed.network_mode == f"container:{g2.id}"
    assert rehomed.id != original_dep_id

    # THE GUARANTEE (ADR-0005): the volume and its data survived remove+recreate.
    survived = client.exec_run(dep, ["cat", "/data/sentinel"])
    assert survived.exit_code == 0
    assert SENTINEL in survived.output
