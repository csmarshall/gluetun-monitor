"""Discovery, interface classification, and the remediation decision tree."""

from __future__ import annotations

from gluetun_monitor.config import Config
from gluetun_monitor.dependents import (
    discover_dependents,
    get_dependents,
    interface_check,
    remediation_action,
)
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.state import InterfaceStatus, RemediationAction

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


def _stack() -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    return fake


def test_discover_by_full_id() -> None:
    fake = _stack()
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    assert discover_dependents(fake, "gluetun") == ["qbittorrent"]


def test_discover_by_name_form() -> None:
    fake = _stack()
    fake.add_container("sonarr", network_mode="container:gluetun")
    assert discover_dependents(fake, "gluetun") == ["sonarr"]


def test_discover_by_short_id_prefix() -> None:
    fake = _stack()
    fake.add_container("radarr", network_mode=f"container:{GLUETUN_ID[:12]}")
    assert discover_dependents(fake, "gluetun") == ["radarr"]


def test_discover_excludes_gluetun_and_unrelated() -> None:
    fake = _stack()
    fake.add_container("app1", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("unrelated", network_mode="bridge")
    found = discover_dependents(fake, "gluetun")
    assert found == ["app1"]


def test_discover_skips_stopped_containers() -> None:
    fake = _stack()
    fake.add_container("running-dep", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("stopped-dep", network_mode=f"container:{GLUETUN_ID}", running=False)
    assert discover_dependents(fake, "gluetun") == ["running-dep"]


def test_get_dependents_manual_list() -> None:
    fake = FakeDockerClient()
    cfg = Config(dependent_containers=" app1 , app2,app3 ")
    logger = _NullLogger()
    assert get_dependents(fake, cfg, logger) == ["app1", "app2", "app3"]


def _exec_returns(output: str, code: int = 0):
    return lambda name, cmd: ExecResult(code, output)


def test_interface_check_live() -> None:
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("eth0\nlo\ntun0\n")
    assert interface_check(fake, "dep") is InterfaceStatus.LIVE


def test_interface_check_stranded() -> None:
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("lo\n")
    assert interface_check(fake, "dep") is InterfaceStatus.STRANDED


def test_interface_check_unknown_on_exec_failure() -> None:
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("", code=1)
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_interface_check_unknown_on_empty_output() -> None:
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("   \n")
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_remediation_restart_when_id_matches() -> None:
    fake = _stack()
    info = fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RESTART


def test_remediation_recreate_when_id_differs() -> None:
    fake = _stack()
    old_id = "b" * 64
    info = fake.add_container("dep", network_mode=f"container:{old_id}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RECREATE


def test_remediation_restart_short_id_match() -> None:
    fake = _stack()
    info = fake.add_container("dep", network_mode=f"container:{GLUETUN_ID[:12]}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RESTART


def test_remediation_try_restart_name_form() -> None:
    fake = _stack()
    info = fake.add_container("dep", network_mode="container:gluetun")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.TRY_RESTART


def test_remediation_try_restart_unexpected_form() -> None:
    fake = _stack()
    info = fake.add_container("dep", network_mode="bridge")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.TRY_RESTART


class _NullLogger:
    def info(self, m: str) -> None: ...
    def warn(self, m: str) -> None: ...
    def error(self, m: str) -> None: ...
    def debug(self, m: str) -> None: ...
    def check(self, m: str) -> None: ...
    def endpoint(self, m: str) -> None: ...
