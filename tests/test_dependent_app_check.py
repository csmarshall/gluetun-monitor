"""Monitor.check_dependent_app + its wiring into the restart/reverify path (ADR-0018).

Why: this is the actual decision logic (threshold gating, short-circuit-on-first-
failure, the zero-match warning, and — the whole point of the ADR — that a breach
here restarts gluetun and gets re-verified the same as a root-site breach, without
polluting the per-site stats sidecar with a non-SiteSpec attribution string).
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import _UNPROBEABLE_ALERT_LOOPS, Monitor

from .fakes import FakeDockerClient, FakeNotifier

GLUETUN_ID = "a" * 64


@pytest.fixture
def sites_file(tmp_path: Path) -> str:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    return str(conf)


def _app_checks_file(tmp_path: Path, contents: str) -> str:
    conf = tmp_path / "app-checks.conf"
    conf.write_text(contents)
    return str(conf)


def _monitor(
    fake: FakeDockerClient, sites_file: str, *, notifier: FakeNotifier | None = None, **cfg_overrides
) -> Monitor:
    cfg = Config(config_file=sites_file, gluetun_container="gluetun", **cfg_overrides)
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    return Monitor(
        fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None, notifier=notifier
    )


def _ok(name: str, cmd: list[str]) -> ExecResult:
    return ExecResult(0, "  HTTP/1.1 200 OK\n")


def _forbidden(name: str, cmd: list[str]) -> ExecResult:
    return ExecResult(8, "  HTTP/1.1 403 Forbidden\n")


# ----- check_dependent_app unit tests -----


def test_passing_check_is_not_breached(tmp_path: Path, sites_file: str) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep-1", network_mode=f"container:{GLUETUN_ID}")
    fake.on_exec = _ok
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
    )
    assert m.check_dependent_app(["dep-1"]) == []
    assert m.app_check_failures.get("^dep-.*$ -> https://provider.example") == 0


def test_failure_below_threshold_does_not_breach(tmp_path: Path, sites_file: str) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = _forbidden
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=2,
    )
    assert m.check_dependent_app(["dep-1"]) == []  # 1st failure, threshold 2
    assert m.check_dependent_app(["dep-1"]) != []  # 2nd failure -> breach


def test_breach_attribution_names_container_and_status(tmp_path: Path, sites_file: str) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = _forbidden
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
    )
    breached = m.check_dependent_app(["dep-1"])
    assert len(breached) == 1
    assert "^dep-.*$" in breached[0]
    assert "dep-1" in breached[0]
    assert "403" in breached[0]


def test_per_rule_failures_override(tmp_path: Path, sites_file: str) -> None:
    """|failures=1 on the rule overrides DEPENDENT_APP_CHECK_FAILURES=5."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = _forbidden
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(
            tmp_path, "^dep-.*$ https://provider.example|failures=1\n"
        ),
        dependent_app_check_failures=5,
    )
    assert m.check_dependent_app(["dep-1"]) != []  # breaches on the very first failure


def test_exec_failed_is_skipped_not_counted(tmp_path: Path, sites_file: str) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)

    def handler(name: str, cmd: list[str]) -> ExecResult:
        raise RuntimeError("container gone")

    fake.on_exec = handler
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
    )
    assert m.check_dependent_app(["dep-1"]) == []
    assert m.app_check_failures.get("^dep-.*$ -> https://provider.example") == 0


def test_short_circuits_on_first_failure(tmp_path: Path, sites_file: str) -> None:
    """A rule matching several containers stops at the first real failure —
    the rest are never probed (ADR-0018: 'can ANY matched container not reach
    this site' needs only one answer)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    probed: list[str] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        probed.append(name)
        return ExecResult(8, "  HTTP/1.1 403 Forbidden\n")

    fake.on_exec = handler
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
    )
    breached = m.check_dependent_app(["dep-1", "dep-2", "dep-3"])
    assert breached != []
    assert probed == ["dep-1"]  # dep-2/dep-3 never probed


def test_zero_match_warns_once_after_streak(tmp_path: Path, sites_file: str) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.on_exec = _ok
    log_stream = io.StringIO()
    cfg = Config(
        config_file=sites_file, gluetun_container="gluetun",
        app_checks_file=_app_checks_file(tmp_path, "^no-such-container$ https://provider.example\n"),
    )
    m = Monitor(fake, cfg, Logger(log_file=None, level="DEBUG", stream=log_stream),
                rng=random.Random(0), sleep=lambda _s: None)
    for _ in range(_UNPROBEABLE_ALERT_LOOPS - 1):
        m.check_dependent_app([])
    assert "matched no running dependent" not in log_stream.getvalue()
    m.check_dependent_app([])
    assert "matched no running dependent" in log_stream.getvalue()


# ----- full run_once integration -----


def test_app_check_breach_restarts_gluetun_and_skips_stats(tmp_path: Path, sites_file: str) -> None:
    """A healthy root site set + a failing app-check rule still restarts gluetun
    (the whole point of ADR-0018), but the synthetic attribution string never
    reaches the per-site stats sidecar (deliberately deferred)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep-1", network_mode=f"container:{GLUETUN_ID}")

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if cmd and cmd[-1] == "https://provider.example":
            return ExecResult(8, "  HTTP/1.1 403 Forbidden\n")
        return ExecResult(0, "")  # root sites + dependent viability all pass

    fake.on_exec = handler
    m = _monitor(
        fake, sites_file,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
    )
    m.run_once()
    assert "gluetun" in fake.restarted
    assert not any("app-check" in key for key in m.stats.sites)


def test_reverify_after_restart_rechecks_app_check(tmp_path: Path, sites_file: str) -> None:
    """After the restart, the app-check must be re-run: it started failing, and
    stays failing post-restart here -> the monitor must report unrecovered, not
    a false 'recovered' (Tenet 7)."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep-1", network_mode=f"container:{GLUETUN_ID}")
    notifier = FakeNotifier()

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if cmd and cmd[-1] == "https://provider.example":
            return ExecResult(8, "  HTTP/1.1 403 Forbidden\n")  # still blocked after restart
        return ExecResult(0, "")

    fake.on_exec = handler
    m = _monitor(
        fake, sites_file, notifier=notifier,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
        apprise_urls=("json://example.test",),
    )
    m.run_once()
    assert "gluetun" in fake.restarted
    bodies = " ".join(e.body for e in notifier.events)
    assert "still failing" in bodies
    assert "app-check" in bodies


def test_reverify_recovers_when_app_check_now_passes(tmp_path: Path, sites_file: str) -> None:
    """The mirror case: the app-check triggered the restart, and after it the
    site is reachable again -> genuinely recovered, not held open forever."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    fake.add_container("dep-1", network_mode=f"container:{GLUETUN_ID}")
    notifier = FakeNotifier()

    def handler(name: str, cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["ls", "/sys/class/net"]:
            return ExecResult(0, "eth0\nlo\n")
        if cmd and cmd[-1] == "https://provider.example":
            # Fails until gluetun has been restarted, then passes (new exit endpoint).
            if fake.restarted:
                return ExecResult(0, "  HTTP/1.1 200 OK\n")
            return ExecResult(8, "  HTTP/1.1 403 Forbidden\n")
        return ExecResult(0, "")

    fake.on_exec = handler
    m = _monitor(
        fake, sites_file, notifier=notifier,
        app_checks_file=_app_checks_file(tmp_path, "^dep-.*$ https://provider.example\n"),
        dependent_app_check_failures=1,
        apprise_urls=("json://example.test",),
    )
    m.run_once()
    assert "gluetun" in fake.restarted
    bodies = " ".join(e.body for e in notifier.events)
    assert "still failing" not in bodies
    assert any("healthy again" in e.body for e in notifier.events)
