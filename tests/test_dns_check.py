"""The getaddrinfo DNS cascade (wget -> getent -> ping) and its three outcomes.

Why: this is the per-dependent DNS validator. It must (a) prefer the faithful
tool that's present, (b) cascade past tools the container lacks, (c) return
UNVALIDATED (not a false pass/fail) when no tool exists, and (d) read each tool's
output correctly — especially that a resolved-but-unreachable ping (blocked ICMP)
is still a DNS success.
"""

from __future__ import annotations

from gluetun_monitor.dns_check import DnsStatus, validate_dns
from gluetun_monitor.docker_client import ExecResult

from .fakes import FakeDockerClient


def _client(handler) -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.add_container("dep", network_mode="container:x")
    fake.on_exec = handler
    return fake


# ----- tool 1: wget present (the common case) -----


def test_wget_resolves_ok() -> None:
    """wget gets an HTTP response → DNS resolved → OK via wget (no cascade)."""
    fake = _client(lambda n, c: ExecResult(0, "  HTTP/1.1 200 OK\n"))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.OK and r.tool == "wget"


def test_wget_404_is_ok() -> None:
    """busybox wget 404 (exit 1, responded) → DNS resolved → OK."""
    fake = _client(lambda n, c: ExecResult(1, "  HTTP/1.1 404 Not Found\n"))
    assert validate_dns(fake, "dep", "https://x", "x", 5).status is DnsStatus.OK


def test_wget_dns_failure_is_broken() -> None:
    """wget 'bad address' → DNS broken → BROKEN via wget."""
    fake = _client(lambda n, c: ExecResult(1, "wget: bad address 'x'\n"))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.BROKEN and r.tool == "wget"


def test_wget_resolved_but_no_http_is_ok() -> None:
    """wget resolved DNS but couldn't complete (connection refused) → still OK
    (DNS is the only per-container fault), and the reason says so honestly rather
    than claiming a full connection."""
    fake = _client(lambda n, c: ExecResult(1, "Connecting to x (1.2.3.4)\n"
                                              "wget: can't connect: Connection refused\n"))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.OK and r.tool == "wget"
    assert "no HTTP response" in r.reason


def test_wget_broken_short_circuits_the_cascade() -> None:
    """A definitive DNS failure from wget must NOT fall through to getent/ping —
    we already know resolution failed. (getent here would say OK if consulted.)"""
    fake = _client(_cascade({
        "wget": ExecResult(1, "wget: bad address 'x'\n"),
        "getent": ExecResult(0, "1.2.3.4 x\n"),  # would say OK if (wrongly) consulted
    }))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.BROKEN and r.tool == "wget"


# ----- cascade past an absent wget -----


def _cascade(responses: dict[str, ExecResult]):
    """Handler that returns a canned result per leading command word, or a
    'not found' (exit 127) for anything not in the map (simulating an absent tool)."""
    def handler(name: str, cmd: list[str]) -> ExecResult:
        return responses.get(cmd[0], ExecResult(127, f"exec: {cmd[0]}: executable file not found"))
    return handler


def test_falls_through_wget_absent_to_getent_ok() -> None:
    """No wget → cascade to getent, which resolves → OK via getent."""
    fake = _client(_cascade({"getent": ExecResult(0, "1.2.3.4  x\n")}))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.OK and r.tool == "getent"


def test_getent_name_not_found_is_broken() -> None:
    """getent exit 2 = name not found → BROKEN."""
    fake = _client(_cascade({"getent": ExecResult(2, "")}))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.BROKEN and r.tool == "getent"


def test_falls_through_to_ping_resolved_despite_blocked_icmp() -> None:
    """No wget/getent → ping; ICMP blocked (permission denied) but the name still
    resolved → OK (we care about resolution, not reachability)."""
    fake = _client(_cascade({
        "ping": ExecResult(1, "PING x (1.2.3.4): 56 data bytes\nping: permission denied\n"),
    }))
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.OK and r.tool == "ping"


def test_ping_resolution_failure_is_broken() -> None:
    fake = _client(_cascade({"ping": ExecResult(1, "ping: bad address 'x'\n")}))
    assert validate_dns(fake, "dep", "https://x", "x", 5).status is DnsStatus.BROKEN


def test_no_tools_is_unvalidated() -> None:
    """Distroless-style: nothing present → UNVALIDATED (don't guess)."""
    fake = _client(_cascade({}))  # every command -> not found
    r = validate_dns(fake, "dep", "https://x", "x", 5)
    assert r.status is DnsStatus.UNVALIDATED and r.tool == ""


def test_exec_exception_treated_as_absent_then_unvalidated() -> None:
    """If every exec raises, no tool could run → UNVALIDATED, not a crash."""
    def boom(name: str, cmd: list[str]) -> ExecResult:
        raise RuntimeError("daemon hiccup")

    fake = _client(boom)
    assert validate_dns(fake, "dep", "https://x", "x", 5).status is DnsStatus.UNVALIDATED
