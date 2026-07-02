"""Durable dependent memory: id-history adoption + persistence (#97).

Why: the exact failure observed live (twice, in both forms) — a monitor restart
followed closely by a gluetun recreate left every dependent stranded AND
invisible: auto-discovery matches only running containers on the CURRENT
gluetun id, and the remembered set was in-memory only. These tests pin the fix:
the persisted gluetun id history makes a dead parent *confirmable* (Docker ids
are never reused), so stranded containers — running or exited — are adopted and
healed; dead parents NOT in the history keep the warn-only behavior (pinned in
test_orphan_warn.py); the id history prunes by reference, never by count.
"""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.monitor_state import MonitorState

from .fakes import FakeDockerClient

OLD_ID = "a" * 64
NEW_ID = "b" * 64
OTHER_ID = "e" * 64  # a dead parent that was never gluetun


def _monitor(
    fake: FakeDockerClient, state_file: Path, sites_file: Path, **cfg: object
) -> tuple[Monitor, io.StringIO]:
    sites_file.write_text("https://a.example\n")
    cfg.setdefault("config_file", str(sites_file))
    cfg.setdefault("gluetun_container", "gluetun")
    cfg.setdefault("monitor_state_file", str(state_file))
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    mon = Monitor(
        fake, Config(**cfg), logger, rng=random.Random(0), sleep=lambda _s: None
    )

    def running_aware(name: str, cmd: list[str]) -> ExecResult:
        # Real docker refuses exec into a non-running container — the probe
        # relies on that to classify a stopped dependent as not-running.
        info = fake.inspect(name)
        if info is None or not info.running:
            return ExecResult(126, "container is not running")
        return ExecResult(0, "eth0\nlo\ntun0\n")

    fake.on_exec = running_aware
    return mon, stream


# ----- MonitorState: persistence contract -----


def test_state_round_trips(tmp_path: Path) -> None:
    st = MonitorState(str(tmp_path / "state.json"))
    st.record_gluetun_id(OLD_ID)
    st.record_gluetun_id(NEW_ID)  # newest first
    st.set_known_dependents({"dep1", "dep2"})
    assert st.save()
    reloaded = MonitorState(str(tmp_path / "state.json"))
    assert reloaded.gluetun_ids == [NEW_ID, OLD_ID]
    assert reloaded.known_dependents == {"dep1", "dep2"}


def test_state_file_carries_observability_fields_only_as_extras(tmp_path: Path) -> None:
    """schema_version/updated_at are present for humans and future migration —
    and are never read back as decision inputs (reload ignores them)."""
    st = MonitorState(str(tmp_path / "state.json"))
    st.record_gluetun_id(OLD_ID)
    st.save()
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["schema_version"] == 1
    assert "updated_at" in data


def test_corrupt_state_degrades_to_empty_memory(tmp_path: Path) -> None:
    """State must never take monitoring down (#73 principle): any shape of
    garbage — not just truncation — loads as empty memory."""
    p = tmp_path / "state.json"
    for garbage in ('{"gluetun_ids": "not-a-list"', '["top", "level", "list"]', "ÿØÿà"):
        p.write_text(garbage)
        st = MonitorState(str(p))
        assert st.gluetun_ids == [] and st.known_dependents == set()


def test_save_is_write_on_change_only(tmp_path: Path) -> None:
    st = MonitorState(str(tmp_path / "state.json"))
    st.record_gluetun_id(OLD_ID)
    assert st.save() is True
    assert st.save() is False  # clean — nothing to write
    st.record_gluetun_id(OLD_ID)  # already newest — not a change
    assert st.save() is False


# ----- THE regression: monitor restart + gluetun recreate window -----


def test_restarted_monitor_heals_exited_stranded_dependent(tmp_path: Path) -> None:
    """The production failure, exited form: state remembers the OLD gluetun id;
    a fresh monitor (empty in-RAM memory) finds the dependent Exited on that
    dead id → adopted and RECREATED in the first loop."""
    prior = MonitorState(str(tmp_path / "state.json"))
    prior.record_gluetun_id(OLD_ID)
    prior.save()

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("dep", network_mode=f"container:{OLD_ID}", running=False)
    mon, stream = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon.run_once()

    assert "Adopting stranded dependent 'dep'" in stream.getvalue()
    assert fake.created and fake.created[0][1] == "dep"  # recreated under its name
    assert f"container:{NEW_ID}" in json.dumps(fake.created[0][0])  # re-homed


def test_restarted_monitor_heals_running_stranded_dependent(tmp_path: Path) -> None:
    """The running-but-loopback form: same adoption, remediated via the probe
    path (STRANDED interface check → recreate)."""
    prior = MonitorState(str(tmp_path / "state.json"))
    prior.record_gluetun_id(OLD_ID)
    prior.save()

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("dep", network_mode=f"container:{OLD_ID}", running=True)
    mon, stream = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")

    def loopback_when_stranded(name: str, cmd: list[str]) -> ExecResult:
        info = fake.inspect(name)
        if info is None or not info.running:
            return ExecResult(126, "container is not running")
        if info.network_mode == f"container:{OLD_ID}" and cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "lo\n")  # stranded: loopback only
        return ExecResult(0, "eth0\nlo\ntun0\n")

    fake.on_exec = loopback_when_stranded
    mon.run_once()

    assert "Adopting stranded dependent 'dep'" in stream.getvalue()
    assert fake.created and fake.created[0][1] == "dep"


def test_dead_parent_not_in_history_is_never_adopted(tmp_path: Path) -> None:
    """Negative pin: a container stranded on a dead id that was NEVER gluetun
    keeps the warn-only behavior — adoption must not re-home someone else's
    container (Tenet 1)."""
    prior = MonitorState(str(tmp_path / "state.json"))
    prior.record_gluetun_id(OLD_ID)
    prior.save()

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("stranger", network_mode=f"container:{OTHER_ID}", running=True)
    mon, stream = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon.run_once()

    out = stream.getvalue()
    assert "Adopting" not in out
    assert "can't be confirmed as gluetun" in out  # the pre-#97 warning
    assert fake.created == []


def test_exited_stranger_is_not_even_warned_about(tmp_path: Path) -> None:
    """An exited container on an unknown dead parent is ordinary leftover junk —
    adopting it would be wrong and warning about it every startup is noise."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("junk", network_mode=f"container:{OTHER_ID}", running=False)
    mon, stream = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon.run_once()
    out = stream.getvalue()
    assert "Adopting" not in out and "junk" not in out
    assert fake.created == []


# ----- memory maintenance -----


def test_known_dependents_survive_a_monitor_restart(tmp_path: Path) -> None:
    """A healthy dependent seen by instance 1 is in instance 2's remembered set
    before its first discovery — the RAM-only gap is closed."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("dep", network_mode=f"container:{NEW_ID}", running=True)
    mon1, _ = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon1.run_once()

    mon2, _ = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    assert "dep" in mon2._known_dependents


def test_gluetun_id_history_prunes_by_reference_not_count(tmp_path: Path) -> None:
    """An id stays while ANY existing container references it — however many
    recreates later — and is dropped the loop after nothing does."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    dep = fake.add_container("dep", network_mode=f"container:{OLD_ID}", running=False)
    mon, _ = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon.mstate.record_gluetun_id(OLD_ID)
    mon.mstate.record_gluetun_id(NEW_ID)

    mon.run_once()  # dep (still referencing OLD_ID pre-recreate) kept it alive…
    # …but the adoption already recreated dep onto NEW_ID, so on the NEXT loop
    # nothing references OLD_ID any more and it is pruned.
    assert dep.id  # (dep was recreated; the old record is gone from the store)
    mon.run_once()
    assert mon.mstate.gluetun_ids == [NEW_ID]


# ----- parked containers are machinery, never dependents (dogfood regression) -----


def test_parked_twin_is_never_adopted(tmp_path: Path) -> None:
    """The #97 dogfood runaway: an unremovable parked twin (dead former-gluetun
    parent) met the adoption scan, was adopted as a 'dependent', re-parked under
    a stacked suffix, and a fresh orphan was minted every loop. Parked names are
    recreate machinery — the scan must skip them entirely."""
    prior = MonitorState(str(tmp_path / "state.json"))
    prior.record_gluetun_id(OLD_ID)
    prior.save()

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container(
        "dep.gm-recreate-old", network_mode=f"container:{OLD_ID}", running=False
    )
    mon, stream = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    mon.run_once()
    mon.run_once()  # a second loop must not stack suffixes either

    out = stream.getvalue()
    assert "Adopting" not in out
    assert fake.created == [] and fake.renamed == []  # no re-park, no minting
    assert not any(
        n.count(".gm-recreate-old") > 1
        for n in (fake.inspect(cid).name for cid in fake.list_all_ids())
    )


def test_parked_name_seeded_from_old_state_is_ignored(tmp_path: Path) -> None:
    """A state file written before the guard may carry parked names in
    known_dependents — they must be filtered at seed time, not remediated."""
    prior = MonitorState(str(tmp_path / "state.json"))
    prior.record_gluetun_id(NEW_ID)
    prior.set_known_dependents({"dep", "dep.gm-recreate-old"})
    prior.save()

    fake = FakeDockerClient()
    fake.add_container("gluetun", id=NEW_ID)
    fake.add_container("dep", network_mode=f"container:{NEW_ID}", running=True)
    fake.add_container(
        "dep.gm-recreate-old", network_mode=f"container:{NEW_ID}", running=False
    )
    mon, _ = _monitor(fake, tmp_path / "state.json", tmp_path / "sites.conf")
    assert "dep.gm-recreate-old" not in mon._known_dependents
    mon.run_once()
    assert "dep.gm-recreate-old" not in mon.mstate.known_dependents


def test_recreate_refuses_a_parked_name(tmp_path: Path) -> None:
    """Belt-and-braces at the recreate layer: even if a parked name reaches
    recreate_dependent, it must refuse rather than stack suffixes."""
    from gluetun_monitor.recreate import recreate_dependent

    fake = FakeDockerClient()
    fake.add_container("dep.gm-recreate-old", network_mode=f"container:{OLD_ID}")
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    assert recreate_dependent(fake, "dep.gm-recreate-old", NEW_ID, logger) is False
    assert fake.renamed == [] and fake.created == []
    assert "machinery" in stream.getvalue()
