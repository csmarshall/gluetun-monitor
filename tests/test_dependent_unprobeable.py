"""#147: a dependent the monitor cannot probe is UNKNOWN — never "ok", never silent.

Docker reports a crash-looping container as ``State.Running: true`` (with
``State.Status: "restarting"``), so a dependent that had died 8,548 times still looked
alive. Exec into it always failed, which correctly left it *unevaluated* — the #137
discipline: never act on a signal you can't attribute. But the heartbeat computed
``healthy = total - failing``, so "couldn't tell" quietly became "fine", and nothing
escalated an unevaluated dependent. The result was ``dependents: 4/4 ok`` printed every
30s for TEN DAYS over a container that had not worked once in that window — the exact
fake-green Tenet 7 exists to forbid.

These tests pin all three halves: the state model (``restarting`` is carried, and both
ContainerInfo constructors agree on it), the arithmetic (unevaluated is subtracted from
the numerator and NEVER hidden from the denominator), and the escalation (a dependent
that stays unprobeable gets said out loud — without ever being restarted, because
restarting a container that is already restarting is churn we can't fix).
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ContainerInfo, ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import _UNPROBEABLE_ALERT_LOOPS, Monitor
from gluetun_monitor.site_stats import SiteStatsStore

from .fakes import FakeDockerClient, FakeNotifier, make_inspect

GLUETUN_ID = "a" * 64
FLARE_ID = "f" * 64
SITE = "https://a.example"

# What the daemon actually says when you exec into a crash-looping container.
_RESTARTING = "Container is restarting, wait until the container is running"


def _monitor(
    tmp_path: Path, notifier: FakeNotifier, log: io.StringIO
) -> tuple[Monitor, FakeDockerClient]:
    """gluetun + two dependents: one healthy, one crash-looping."""
    conf = tmp_path / "sites.conf"
    conf.write_text(f"{SITE}\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID, health="healthy")
    fake.add_container("sonarr", network_mode=f"container:{GLUETUN_ID}")
    # Alive per Docker, healthy per nobody: Running=true AND Status=restarting.
    fake.add_container(
        "flaresolverr", id=FLARE_ID, network_mode=f"container:{GLUETUN_ID}",
        running=True, status="restarting", health="unhealthy",
    )

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if name == "flaresolverr":
            raise RuntimeError(_RESTARTING)  # every exec into it fails, every loop
        if cmd[:1] == ["ls"]:
            return ExecResult(0, "eth0 lo tun0\n")
        if cmd[:1] in (["nslookup"], ["getent"]):
            return ExecResult(0, "1.1.1.1\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    mon = Monitor(
        fake,
        Config(config_file=str(conf), gluetun_container="gluetun",
               fail_threshold=2, dns_wait_timeout=0, advisory_min_restarts=999),
        Logger(log_file=None, stream=log),
        rng=random.Random(0), sleep=lambda _s: None,
        stats=SiteStatsStore(None), notifier=notifier,
    )
    return mon, fake


# ----- the state model: Docker's `Running` is not a health claim -----


def test_inspect_carries_the_crash_loop() -> None:
    """`Running` stays what Docker means by it (alive); `restarting` carries the truth."""
    info = ContainerInfo.from_inspect(
        make_inspect("flaresolverr", id=FLARE_ID, running=True, status="restarting")
    )
    assert info.running is True, "Docker really does report a crash-looper as Running"
    assert info.restarting is True, "...so `restarting` is the only field that catches it"


def test_both_constructors_agree_on_a_crash_looper() -> None:
    """A container's aliveness must not depend on which API call discovered it.

    `/containers/json` lists restarting containers too. Before #147 the list path read
    them as not-running while the inspect path read them as running — the same container,
    two verdicts, decided by which code got there first.
    """
    listed = ContainerInfo.from_list_entry(
        {"Id": FLARE_ID, "Names": ["/flaresolverr"], "State": "restarting", "HostConfig": {}}
    )
    inspected = ContainerInfo.from_inspect(
        make_inspect("flaresolverr", id=FLARE_ID, running=True, status="restarting")
    )
    assert (listed.running, listed.restarting) == (inspected.running, inspected.restarting)
    assert listed.restarting is True


# ----- the arithmetic: unevaluated is not healthy, and is never hidden -----


def test_crash_looping_dependent_is_not_counted_healthy(tmp_path: Path) -> None:
    """The core regression (RED pre-fix: printed `dependents: 2/2 ok`).

    The unprobeable dependent leaves the numerator, stays in the denominator, and is
    named — shrinking the total to `1/1 ok` would just be a fake-green by omission.
    """
    log = io.StringIO()
    mon, _ = _monitor(tmp_path, FakeNotifier(), log)

    mon.run_once()

    out = log.getvalue()
    assert "dependents: 1/2 ok (1 unprobeable)" in out, out
    assert "2/2 ok" not in out, f"fake-green! {out}"


def test_crash_looping_dependent_is_never_restarted(tmp_path: Path) -> None:
    """Restarting a container that is ALREADY restarting fixes nothing and bounces the
    netns its siblings share — the watchdog becoming the outage (Tenets 1 and 7)."""
    log = io.StringIO()
    mon, fake = _monitor(tmp_path, FakeNotifier(), log)

    for _ in range(_UNPROBEABLE_ALERT_LOOPS + 3):
        mon.run_once()

    assert fake.restarted == [], "a crash-looper must be reported, not churned"
    assert (fake.removed, fake.created) == ([], []), "and certainly not recreated"


# ----- the escalation: silence is the bug -----


def test_persistent_unprobeable_dependent_escalates_once(tmp_path: Path) -> None:
    """Ten days of invisibility must not be quiet. Edge-triggered: announced once."""
    notifier = FakeNotifier()
    mon, _ = _monitor(tmp_path, notifier, io.StringIO())

    for _ in range(_UNPROBEABLE_ALERT_LOOPS + 2):
        mon.run_once()

    keys = notifier.event_keys()
    assert keys.count("dependent-unprobeable:flaresolverr") == 1, keys
    assert "dependent-unhealthy:flaresolverr" not in keys, "unknown is not failing (#137)"
    assert "dependent-unprobeable:sonarr" not in keys, "the healthy one stays quiet"


def test_a_brief_unprobeable_blip_does_not_alert(tmp_path: Path) -> None:
    """A remediation restart leaves a dependent unprobeable for a loop or two. That is
    the monitor's own doing, not an incident — the threshold exists to absorb it."""
    notifier = FakeNotifier()
    mon, _ = _monitor(tmp_path, notifier, io.StringIO())

    for _ in range(_UNPROBEABLE_ALERT_LOOPS - 1):
        mon.run_once()

    assert "dependent-unprobeable:flaresolverr" not in notifier.event_keys()


def test_unprobeable_resolves_when_the_dependent_comes_back(tmp_path: Path) -> None:
    """Lifecycle (ADR-0012): the alert clears itself once the container is probeable —
    exactly once, and without a restart, since nothing needed remediating."""
    notifier = FakeNotifier()
    log = io.StringIO()
    mon, fake = _monitor(tmp_path, notifier, log)

    for _ in range(_UNPROBEABLE_ALERT_LOOPS):
        mon.run_once()
    assert "dependent-unprobeable:flaresolverr" in notifier.event_keys()

    # Operator fixes the container: it stops crash-looping and starts answering execs.
    fake._store[FLARE_ID]["State"]["Status"] = "running"

    def probeable(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["ls"]:
            return ExecResult(0, "eth0 lo tun0\n")
        if cmd[:1] in (["nslookup"], ["getent"]):
            return ExecResult(0, "1.1.1.1\n")
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = probeable
    log.truncate(0)
    log.seek(0)
    mon.run_once()

    keys = notifier.event_keys()
    assert keys.count("resolve:dependent-unprobeable:flaresolverr") == 1, keys
    assert fake.restarted == [], "recovery of the probe path must not trigger a restart"
    assert "dependents: 2/2 ok" in log.getvalue(), "and it counts as healthy again"
