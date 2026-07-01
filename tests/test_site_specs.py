"""Per-URL probe tunables (#60): the ``url|key=value`` parsing and how the
resolved override reaches the wget inside gluetun.

Why: a slow-but-alive canary needs a wider timeout than the global default, set
on *that* URL only — without dropping it (the global TIMEOUT stays the floor for
everyone else). These tests pin the forgiving parser (a typo never drops a site)
and prove the override actually lands on the exec'd command end to end.
"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor
from gluetun_monitor.sites import SiteSpec, load_specs, load_specs_report, parse_entry

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


# ----- parse_entry: the forgiving |key=value parser -----

def test_bare_url_has_no_overrides() -> None:
    """A URL with no ``|`` parses exactly as before — both knobs inherit (None)."""
    spec, warnings = parse_entry("https://www.google.com")
    assert spec == SiteSpec("https://www.google.com", timeout=None, tries=None)
    assert warnings == []


def test_single_and_multiple_options() -> None:
    spec, warnings = parse_entry("https://slow.example|timeout=25")
    assert spec == SiteSpec("https://slow.example", timeout=25, tries=None)
    assert warnings == []
    spec, warnings = parse_entry("http://x|timeout=20|tries=2")
    assert spec == SiteSpec("http://x", timeout=20, tries=2)
    assert warnings == []


def test_whitespace_around_url_and_options_is_tolerated() -> None:
    spec, warnings = parse_entry("  https://x | timeout = 30 ")
    assert spec == SiteSpec("https://x", timeout=30)
    assert warnings == []


def test_empty_entry_is_skipped() -> None:
    assert parse_entry("") == (None, [])
    assert parse_entry("   ") == (None, [])
    # An entry that is only options with no URL is nothing to test.
    spec, _ = parse_entry("|timeout=10")
    assert spec is None


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("https://x|nope=5", "unknown option"),
        ("https://x|timeout", "malformed option"),
        ("https://x|timeout=abc", "invalid timeout"),
        ("https://x|timeout=0", "invalid timeout"),
        ("https://x|tries=-1", "invalid tries"),
        # #73: Unicode digits pass str.isdigit() but int() refuses them — must
        # warn-and-skip like any other bad value, never raise (pre-fix this was
        # an uncaught ValueError: a startup crash, or a silent monitoring halt
        # when the file was edited live).
        ("https://x|timeout=²", "invalid timeout"),
        ("https://x|tries=¹²³", "invalid tries"),
    ],
)
def test_bad_options_warn_but_keep_the_url(entry: str, fragment: str) -> None:
    """Forgiving + loud: a bad option is warned about and skipped, never fatal —
    the URL is still monitored on the global defaults (Tenet 1)."""
    spec, warnings = parse_entry(entry)
    assert spec is not None and spec.url == "https://x"
    assert spec.timeout is None and spec.tries is None  # the bad option didn't stick
    assert any(fragment in w for w in warnings)


def test_one_bad_option_does_not_void_a_good_one() -> None:
    spec, warnings = parse_entry("https://x|timeout=25|bogus=1")
    assert spec == SiteSpec("https://x", timeout=25)
    assert len(warnings) == 1 and "unknown option" in warnings[0]


# ----- load_specs / load_specs_report: file + env, dedup, reporting -----

def _conf(tmp_path: Path, text: str) -> str:
    p = tmp_path / "sites.conf"
    p.write_text(text)
    return str(p)


def test_load_specs_from_file_and_env(tmp_path: Path) -> None:
    conf = _conf(tmp_path, "https://a|timeout=25\n# comment\nhttps://b\n")
    specs = load_specs(conf, "https://c|tries=2,https://d")
    by_url = {s.url: s for s in specs}
    assert by_url["https://a"].timeout == 25
    assert by_url["https://b"] == SiteSpec("https://b")
    assert by_url["https://c"].tries == 2
    assert by_url["https://d"] == SiteSpec("https://d")


def test_dedup_by_url_keeps_first_occurrence(tmp_path: Path) -> None:
    """A URL repeated (file then env) keeps the *first* options — file wins."""
    conf = _conf(tmp_path, "https://a|timeout=25\n")
    specs = load_specs(conf, "https://a|timeout=99")
    assert [s.url for s in specs] == ["https://a"]
    assert specs[0].timeout == 25


def test_report_surfaces_option_warnings_and_unsafe_drops(tmp_path: Path) -> None:
    conf = _conf(tmp_path, "https://good|bogus=1\n-flaggy\n")
    specs, rejected = load_specs_report(conf, None)
    assert [s.url for s in specs] == ["https://good"]  # bad option kept the URL
    reasons = dict(rejected)
    assert "unknown option" in reasons["https://good"]
    assert "flag" in reasons["-flaggy"]  # leading-dash URL dropped as unsafe


def test_missing_file_is_not_fatal(tmp_path: Path) -> None:
    specs = load_specs(str(tmp_path / "nope.conf"), "https://only-env")
    assert [s.url for s in specs] == ["https://only-env"]


# ----- reload resilience (#73): a bad edit or reload failure never halts checks -----

def _probing_monitor(conf: str) -> tuple[Monitor, list[list[str]], io.StringIO]:
    """A Monitor against a healthy fake gluetun that records every exec'd command."""
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    stream = io.StringIO()
    cfg = Config(config_file=conf, gluetun_container="gluetun")
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    return (
        Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None),
        cmds,
        stream,
    )


def _wget_urls(cmds: list[list[str]]) -> list[str]:
    return [c[-1] for c in cmds if c and c[0] == "wget" and c[-1].startswith("http")]


def test_bad_live_edit_does_not_halt_monitoring(tmp_path: Path) -> None:
    """#73 regression: a live sites.conf edit that adds a Unicode-digit tunable
    (`isdigit()`-true, `int()`-false) must warn-and-skip, not raise — pre-fix the
    ValueError escaped run_once every loop and monitoring silently ceased."""
    conf = _conf(tmp_path, "https://a.example\n")
    mon, cmds, _ = _probing_monitor(conf)
    mon.run_once()
    Path(conf).write_text("https://a.example|timeout=²\n")
    mon.run_once()  # pre-fix: uncaught ValueError
    # Both loops probed the site; the bad override fell back to the global knobs.
    assert _wget_urls(cmds).count("https://a.example") == 2


def test_reload_failure_keeps_previous_sites_and_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth (#73): if the per-loop sites reload fails for ANY reason
    (unreadable bind-mount, a future parse bug), the loop must fall back to the
    last good site set and keep checking — a stale list beats a silent halt."""
    conf = _conf(tmp_path, "https://a.example\n")
    mon, cmds, stream = _probing_monitor(conf)
    mon.run_once()  # loads the good set

    def boom(*_a: object) -> list[SiteSpec]:
        raise OSError("bind-mount blip")

    monkeypatch.setattr("gluetun_monitor.monitor.load_specs", boom)
    mon.run_once()
    assert _wget_urls(cmds).count("https://a.example") == 2  # still probing
    assert "Failed to reload sites config" in stream.getvalue()


# ----- end to end: the override reaches the wget inside gluetun -----

def test_per_url_timeout_lands_on_the_probe(tmp_path: Path) -> None:
    """The override is not just parsed — it reaches the exec'd wget for that URL
    only, while every other site keeps the global TIMEOUT."""
    conf = _conf(tmp_path, "https://fast.example\nhttps://slow.example|timeout=25|tries=3\n")
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    cfg = Config(config_file=conf, gluetun_container="gluetun", timeout=10, wget_tries=1)
    logger = Logger(log_file=None, level="DEBUG", stream=io.StringIO())
    Monitor(fake, cfg, logger, rng=random.Random(0), sleep=lambda _s: None).run_once()

    probes = {c[-1]: c for c in cmds if c and c[0] == "wget" and c[-1].startswith("http")}
    assert "--timeout=25" in probes["https://slow.example"]
    assert "--tries=3" in probes["https://slow.example"]
    assert "--timeout=10" in probes["https://fast.example"]  # global, untouched
    assert "--tries=1" in probes["https://fast.example"]
