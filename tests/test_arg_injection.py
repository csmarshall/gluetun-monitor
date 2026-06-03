"""A leading-dash site can't be turned into a wget/ping/getent option.

Why (Tenet 1 — first do no harm): site URLs come from sites.conf / SITES, which
an operator may template or share. An entry like ``--directory-prefix=/etc`` is
parsed by GNU wget as a flag, not a URL — it could write files inside a probed
container and corrupts the connectivity signal. Two layers close this: the exec
arg-lists put ``--`` before the URL/host, and sites parsing drops leading-dash /
hostless entries with a warning rather than probing them.
"""

from __future__ import annotations

from pathlib import Path

from gluetun_monitor.connectivity import probe_site
from gluetun_monitor.dns_check import validate_dns
from gluetun_monitor.docker_client import ExecResult
from gluetun_monitor.sites import load_sites, load_sites_report, unsafe_site_reason

from .fakes import FakeDockerClient


def _capture() -> tuple[FakeDockerClient, list[list[str]]]:
    fake = FakeDockerClient()
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(0, "  HTTP/1.1 200 OK\n")

    fake.on_exec = handler
    return fake, cmds


def _end_of_options(cmd: list[str], value: str) -> bool:
    """The value appears, and a ``--`` separator precedes it."""
    return value in cmd and "--" in cmd[: cmd.index(value)]


def test_probe_site_puts_url_after_end_of_options() -> None:
    fake, cmds = _capture()
    probe_site(fake, "dep", "--directory-prefix=/etc", timeout=10)
    assert _end_of_options(cmds[0], "--directory-prefix=/etc")


def test_validate_dns_guards_wget_getent_ping() -> None:
    """All three cascade tools get the host after ``--`` (none present -> full
    cascade, so we see every command)."""
    fake = FakeDockerClient()
    cmds: list[list[str]] = []

    def handler(name: str, cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        if cmd and cmd[0] == "wget":
            return ExecResult(-1, "")  # absent -> fall through to getent
        if cmd[:2] == ["getent", "hosts"]:
            return ExecResult(127, "not found")  # absent -> fall through to ping
        return ExecResult(1, "")  # ping ran, no resolution

    fake.on_exec = handler
    validate_dns(fake, "dep", "http://-evil.example", "-evil.example", timeout=5)
    wget = next(c for c in cmds if c and c[0] == "wget")
    getent = next(c for c in cmds if c[:2] == ["getent", "hosts"])
    ping = next(c for c in cmds if c and c[0] == "ping")
    assert _end_of_options(wget, "http://-evil.example")
    assert _end_of_options(getent, "-evil.example")
    assert _end_of_options(ping, "-evil.example")


def test_unsafe_site_reason() -> None:
    assert unsafe_site_reason("--output-document=/x") is not None
    assert unsafe_site_reason("-x") is not None
    assert unsafe_site_reason("http://") is not None  # no host
    assert unsafe_site_reason("https://www.google.com") is None
    assert unsafe_site_reason("1.1.1.1") is None  # bare IP is fine


def test_load_sites_drops_unsafe_entries(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://ok.example\n--evil-flag\nhttp://\n", encoding="utf-8")
    safe = load_sites(conf, None)
    assert safe == ["https://ok.example"]


def test_load_sites_report_explains_rejections(tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://ok.example\n--evil-flag\n", encoding="utf-8")
    safe, rejected = load_sites_report(conf, None)
    assert safe == ["https://ok.example"]
    assert rejected and rejected[0][0] == "--evil-flag"
    assert "flag" in rejected[0][1]


def test_unsafe_entries_from_sites_env_are_dropped(tmp_path: Path) -> None:
    safe, rejected = load_sites_report(tmp_path / "missing.conf",
                                       "https://a.example,-bad,--also-bad")
    assert safe == ["https://a.example"]
    assert {e for e, _ in rejected} == {"-bad", "--also-bad"}
