"""Connectivity probe: the wget exit-code map and pass/fail classification.

Why: this is the authoritative "is the tunnel up" signal (ADR-0001/Tenet 2), and
its subtlety is that a site *responding* with an error (auth/4xx/5xx) still means
egress works. Getting the 0/6/8-pass mapping wrong would either miss real outages
or restart gluetun over a harmless 403 — so it's pinned hard (and cross-checked
against the legacy bash in the differential suite).
"""

from __future__ import annotations

import pytest

from gluetun_monitor.connectivity import (
    WGET_PASS_CODES,
    decode_wget_exit_code,
    probe_site,
)
from gluetun_monitor.docker_client import ExecResult

from .fakes import FakeDockerClient


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (0, "Success"),
        (1, "Generic error"),
        (2, "Parse error"),
        (3, "File I/O error"),
        (4, "Network failure (DNS or connection)"),
        (5, "SSL verification failure"),
        (6, "Authentication required"),
        (7, "Protocol error"),
        (8, "Server error (HTTP 4xx/5xx)"),
        (99, "Unknown error (code 99)"),
    ],
)
def test_decode_wget_exit_code(code: int, message: str) -> None:
    """Every wget exit code maps to the same human reason v1.x used (preserved
    verbatim — the differential suite checks this against the bash function)."""
    assert decode_wget_exit_code(code) == message


def test_pass_codes_are_0_6_8() -> None:
    """0 (ok), 6 (auth), 8 (4xx/5xx) count as pass — the site responded, so the
    tunnel is up (Tenet 2). Changing this set changes restart behavior."""
    assert frozenset({0, 6, 8}) == WGET_PASS_CODES


def _client_returning(code: int, output: str = "") -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.on_exec = lambda name, cmd: ExecResult(code, output)
    return fake


def test_site_pass_on_exit_0() -> None:
    """A clean success parses the HTTP code and reports pass."""
    fake = _client_returning(0, "  HTTP/1.1 200 OK\n")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is True
    assert result.http_code == "200"
    assert result.reason == "HTTP 200"


@pytest.mark.parametrize("code", [6, 8])
def test_site_pass_on_responded_error_codes(code: int) -> None:
    """A 403/5xx (codes 6/8) is still a pass: the site answered, so egress works
    — the core "a broken tunnel is not a sad website" distinction (Tenet 2)."""
    fake = _client_returning(code, "  HTTP/1.1 403 Forbidden\n")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is True
    assert "VPN working" in result.reason


@pytest.mark.parametrize("code", [1, 4, 5, 7])
def test_site_fail_on_connectivity_codes(code: int) -> None:
    """DNS/connection/SSL/protocol failures are real failures (the tunnel path is
    broken), reported with the decoded reason."""
    fake = _client_returning(code, "")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is False
    assert result.reason == decode_wget_exit_code(code)


def test_site_parses_last_http_code() -> None:
    """On a redirect chain we report the final status, not the 3xx hop."""
    output = "HTTP/1.1 301 Moved\nLocation: ...\nHTTP/1.1 200 OK\n"
    fake = _client_returning(0, output)
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.http_code == "200"


def test_site_exec_exception_is_a_failure() -> None:
    """If the exec itself blows up (daemon gone mid-probe), that's a failure, not
    an unhandled crash — the loop must survive (Tenet 7)."""
    fake = FakeDockerClient()

    def boom(name: str, cmd: list[str]) -> ExecResult:
        raise RuntimeError("daemon gone")

    fake.on_exec = boom
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is False
    assert "exec failed" in result.reason
