"""The recreate path (ADR-0005) — the highest-risk logic, tested hardest.

build_create_body is pure, so the netns-field surgery and the anonymous-volume
data-preservation guarantee are fully unit-testable against a fixed inspect dict.
"""

from __future__ import annotations

from typing import Any

from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.recreate import build_create_body, recreate_dependent

from .fakes import FakeDockerClient, make_inspect

NEW_GLUETUN_ID = "f" * 64
OLD_GLUETUN_ID = "a" * 64


def _dep_inspect(**overrides: Any) -> dict[str, Any]:
    """A realistic dependent inspect that shared gluetun's (old) netns."""
    raw = make_inspect(
        "qbittorrent",
        id="dep123",
        network_mode=f"container:{OLD_GLUETUN_ID}",
        config={
            "Image": "lscr.io/linuxserver/qbittorrent:latest",
            "Env": ["PUID=1000", "PGID=1000"],
            "Cmd": ["/init"],
            "Labels": {"com.example": "x"},
            "Hostname": "dep123",
            "Domainname": "lan",
            "ExposedPorts": {"8080/tcp": {}},
            "MacAddress": "02:42:ac:11:00:02",
            "Volumes": {"/downloads": {}},
        },
        host_config={
            "PortBindings": {"8080/tcp": [{"HostPort": "8080"}]},
            "PublishAllPorts": False,
            "Dns": ["1.1.1.1"],
            "DnsSearch": ["lan"],
            "DnsOptions": ["ndots:1"],
            "ExtraHosts": ["a:1.2.3.4"],
            "Links": ["other:other"],
            "Binds": ["/host/config:/config"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        mounts=[
            # named volume
            {"Type": "volume", "Name": "qbt_config", "Destination": "/config", "RW": True},
            # anonymous volume (image VOLUME) — the data-loss risk
            {"Type": "volume", "Name": "anon0abc", "Destination": "/downloads", "RW": True},
            # bind mount
            {"Type": "bind", "Source": "/host/media", "Destination": "/media", "RW": False},
        ],
    )
    raw.update(overrides)
    return raw


def test_network_mode_repointed() -> None:
    body = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)
    assert body["HostConfig"]["NetworkMode"] == f"container:{NEW_GLUETUN_ID}"


def test_netns_conflicting_config_fields_stripped() -> None:
    body = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)
    for field in ("Hostname", "Domainname", "ExposedPorts", "MacAddress", "Volumes"):
        assert field not in body, f"{field} should be stripped from Config"


def test_netns_conflicting_hostconfig_fields_stripped() -> None:
    hc = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)["HostConfig"]
    for field in ("PortBindings", "PublishAllPorts", "Dns", "DnsSearch",
                  "DnsOptions", "ExtraHosts", "Links", "Binds"):
        assert field not in hc, f"{field} should be stripped from HostConfig"


def test_preserved_fields_survive() -> None:
    body = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)
    assert body["Image"] == "lscr.io/linuxserver/qbittorrent:latest"
    assert body["Env"] == ["PUID=1000", "PGID=1000"]
    assert body["Cmd"] == ["/init"]
    assert body["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}


def test_anonymous_volume_carried_by_name() -> None:
    """The crux: the anon volume must be re-bound by its existing id, so a
    subsequent `rm` without -v preserves its data."""
    mounts = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)["HostConfig"]["Mounts"]
    anon = [m for m in mounts if m.get("Source") == "anon0abc"]
    assert len(anon) == 1
    assert anon[0] == {
        "Type": "volume", "Source": "anon0abc", "Target": "/downloads", "ReadOnly": False
    }


def test_named_volume_and_bind_carried() -> None:
    mounts = build_create_body(_dep_inspect(), NEW_GLUETUN_ID)["HostConfig"]["Mounts"]
    by_target = {m["Target"]: m for m in mounts}
    assert by_target["/config"] == {
        "Type": "volume", "Source": "qbt_config", "Target": "/config", "ReadOnly": False
    }
    assert by_target["/media"] == {
        "Type": "bind", "Source": "/host/media", "Target": "/media", "ReadOnly": True
    }


def test_tmpfs_mount_carried() -> None:
    raw = _dep_inspect(Mounts=[{"Type": "tmpfs", "Destination": "/tmp"}])
    mounts = build_create_body(raw, NEW_GLUETUN_ID)["HostConfig"]["Mounts"]
    assert mounts == [{"Type": "tmpfs", "Target": "/tmp"}]


def test_networkingconfig_dropped() -> None:
    raw = _dep_inspect()
    raw["NetworkingConfig"] = {"EndpointsConfig": {"bridge": {}}}
    body = build_create_body(raw, NEW_GLUETUN_ID)
    assert "NetworkingConfig" not in body


def test_build_does_not_mutate_input() -> None:
    raw = _dep_inspect()
    before = raw["HostConfig"]["NetworkMode"]
    build_create_body(raw, NEW_GLUETUN_ID)
    assert raw["HostConfig"]["NetworkMode"] == before  # deepcopy, not in-place


def test_no_mounts_means_no_mounts_key() -> None:
    raw = _dep_inspect(Mounts=[])
    hc = build_create_body(raw, NEW_GLUETUN_ID)["HostConfig"]
    assert "Mounts" not in hc


# ----- recreate_dependent orchestration -----


def _logger() -> Logger:
    import io

    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


def test_recreate_dependent_removes_without_volumes_then_creates() -> None:
    fake = FakeDockerClient()
    fake.add(_dep_inspect())
    ok = recreate_dependent(fake, "qbittorrent", NEW_GLUETUN_ID, _logger())
    assert ok is True
    # The data-preservation guarantee: remove must NOT delete volumes.
    assert fake.removed == [("qbittorrent.gm-recreate-old", False)]
    assert len(fake.created) == 1
    body, name = fake.created[0]
    assert name == "qbittorrent"
    assert body["HostConfig"]["NetworkMode"] == f"container:{NEW_GLUETUN_ID}"
    assert len(fake.started) == 1


def test_recreate_dependent_missing_container() -> None:
    fake = FakeDockerClient()
    assert recreate_dependent(fake, "ghost", NEW_GLUETUN_ID, _logger()) is False
    assert fake.removed == []
