"""Connectivity probe: the wget exit-code map and pass/fail classification."""

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
    assert decode_wget_exit_code(code) == message


def test_pass_codes_are_0_6_8() -> None:
    assert frozenset({0, 6, 8}) == WGET_PASS_CODES


def _client_returning(code: int, output: str = "") -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.on_exec = lambda name, cmd: ExecResult(code, output)
    return fake


def test_site_pass_on_exit_0() -> None:
    fake = _client_returning(0, "  HTTP/1.1 200 OK\n")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is True
    assert result.http_code == "200"
    assert result.reason == "HTTP 200"


@pytest.mark.parametrize("code", [6, 8])
def test_site_pass_on_responded_error_codes(code: int) -> None:
    fake = _client_returning(code, "  HTTP/1.1 403 Forbidden\n")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is True
    assert "VPN working" in result.reason


@pytest.mark.parametrize("code", [1, 4, 5, 7])
def test_site_fail_on_connectivity_codes(code: int) -> None:
    fake = _client_returning(code, "")
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is False
    assert result.reason == decode_wget_exit_code(code)


def test_site_parses_last_http_code() -> None:
    output = "HTTP/1.1 301 Moved\nLocation: ...\nHTTP/1.1 200 OK\n"
    fake = _client_returning(0, output)
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.http_code == "200"


def test_site_exec_exception_is_a_failure() -> None:
    fake = FakeDockerClient()

    def boom(name: str, cmd: list[str]) -> ExecResult:
        raise RuntimeError("daemon gone")

    fake.on_exec = boom
    result = probe_site(fake, "gluetun", "https://example.com", 10)
    assert result.ok is False
    assert "exec failed" in result.reason
