"""Discovery, interface classification, and the remediation decision tree.

Why: these three feed the #20 fix. Discovery decides *who* is watched; the
interface check is the ground-truth strand signal (ADR-0004); and
remediation_action is the load-bearing restart-vs-recreate decision — picking the
wrong branch either fails to heal (restart into a dead netns) or recreates
needlessly. The id-vs-name cases below are exactly the ambiguities ADR-0004 calls out.
"""

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
    """A dependent whose NetworkMode is the resolved full id (the compose
    `service:` form) is discovered."""
    fake = _stack()
    fake.add_container("qbittorrent", network_mode=f"container:{GLUETUN_ID}")
    assert discover_dependents(fake, "gluetun") == ["qbittorrent"]


def test_discover_by_name_form() -> None:
    """The `container:<name>` form is discovered too (some setups write the name)."""
    fake = _stack()
    fake.add_container("sonarr", network_mode="container:gluetun")
    assert discover_dependents(fake, "gluetun") == ["sonarr"]


def test_discover_by_short_id_prefix() -> None:
    """The 12-char short-id prefix form is matched."""
    fake = _stack()
    fake.add_container("radarr", network_mode=f"container:{GLUETUN_ID[:12]}")
    assert discover_dependents(fake, "gluetun") == ["radarr"]


def test_discover_excludes_gluetun_and_unrelated() -> None:
    """gluetun itself and bridge-networked containers are never treated as
    dependents (no false positives)."""
    fake = _stack()
    fake.add_container("app1", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("unrelated", network_mode="bridge")
    found = discover_dependents(fake, "gluetun")
    assert found == ["app1"]


def test_discover_skips_stopped_containers() -> None:
    """Discovery considers only running containers (it lists by `docker ps`)."""
    fake = _stack()
    fake.add_container("running-dep", network_mode=f"container:{GLUETUN_ID}")
    fake.add_container("stopped-dep", network_mode=f"container:{GLUETUN_ID}", running=False)
    assert discover_dependents(fake, "gluetun") == ["running-dep"]


def test_get_dependents_manual_list() -> None:
    """A manual DEPENDENT_CONTAINERS value is parsed (trimmed, blanks dropped)."""
    fake = FakeDockerClient()
    cfg = Config(dependent_containers=" app1 , app2,app3 ")
    logger = _NullLogger()
    assert get_dependents(fake, cfg, logger) == ["app1", "app2", "app3"]


def _exec_returns(output: str, code: int = 0):
    return lambda name, cmd: ExecResult(code, output)


def test_interface_check_live() -> None:
    """A non-loopback interface (eth0/tun0 present) → LIVE (eligible worker)."""
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("eth0\nlo\ntun0\n")
    assert interface_check(fake, "dep") is InterfaceStatus.LIVE


def test_interface_check_stranded() -> None:
    """Only `lo` → STRANDED — the exact #20 loopback-only state."""
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("lo\n")
    assert interface_check(fake, "dep") is InterfaceStatus.STRANDED


def test_interface_check_unknown_on_exec_failure() -> None:
    """A failed exec (no shell / distroless) → UNKNOWN, routing to the inspect
    fallback rather than a wrong LIVE/STRANDED guess."""
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("", code=1)
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_interface_check_unknown_on_empty_output() -> None:
    """Empty output is inconclusive → UNKNOWN, not a false STRANDED."""
    fake = FakeDockerClient()
    fake.on_exec = _exec_returns("   \n")
    assert interface_check(fake, "dep") is InterfaceStatus.UNKNOWN


def test_remediation_restart_when_id_matches() -> None:
    """NetworkMode target == current gluetun id → restart (it can rejoin)."""
    fake = _stack()
    info = fake.add_container("dep", network_mode=f"container:{GLUETUN_ID}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RESTART


def test_remediation_recreate_when_id_differs() -> None:
    """Target is a *different* id (gluetun was recreated) → recreate; a restart
    would fail into a dead netns (the #20 B1 case)."""
    fake = _stack()
    old_id = "b" * 64
    info = fake.add_container("dep", network_mode=f"container:{old_id}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RECREATE


def test_remediation_restart_short_id_match() -> None:
    """A short-id target that prefixes the current full id still counts as a match
    → restart."""
    fake = _stack()
    info = fake.add_container("dep", network_mode=f"container:{GLUETUN_ID[:12]}")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.RESTART


def test_remediation_try_restart_name_form() -> None:
    """A name-form target (`container:gluetun`) is ambiguous (can't compare ids)
    → try-restart-then-escalate, per ADR-0004's untested-name branch."""
    fake = _stack()
    info = fake.add_container("dep", network_mode="container:gluetun")
    assert remediation_action(info, GLUETUN_ID) is RemediationAction.TRY_RESTART


def test_remediation_try_restart_unexpected_form() -> None:
    """An unexpected NetworkMode (not container:*) defaults to the safe
    try-restart path rather than a recreate."""
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
