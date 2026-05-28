"""Connectivity probing — ``wget --spider`` from inside a container's netns.

This is the ADR-0001 authoritative test, preserved verbatim from v1.x: the same
``wget --spider -S`` invocation, the same exit-code -> pass/fail map (0/6/8 mean
the site responded => the VPN is up). Only the dispatch moved from a shell
background job to ``DockerClient.exec_run``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .docker_client import DockerClient

# wget exit codes where the site *responded* — the tunnel is therefore working:
#   0 = success, 6 = auth required, 8 = HTTP 4xx/5xx. Everything else is a real
# connectivity failure (4 = DNS/connect, 5 = SSL, ...).
WGET_PASS_CODES = frozenset({0, 6, 8})

_WGET_EXIT_MESSAGES: dict[int, str] = {
    0: "Success",
    1: "Generic error",
    2: "Parse error",
    3: "File I/O error",
    4: "Network failure (DNS or connection)",
    5: "SSL verification failure",
    6: "Authentication required",
    7: "Protocol error",
    8: "Server error (HTTP 4xx/5xx)",
}

_HTTP_CODE_RE = re.compile(r"HTTP/[0-9.]+\s+(\d+)")


def decode_wget_exit_code(code: int) -> str:
    """Human-readable reason for a wget exit code (v1.x ``decode_wget_exit_code``)."""
    return _WGET_EXIT_MESSAGES.get(code, f"Unknown error (code {code})")


def _parse_http_code(output: str) -> str:
    """Last HTTP status code seen in ``wget -S`` output, or 'N/A'."""
    matches = _HTTP_CODE_RE.findall(output)
    return matches[-1] if matches else "N/A"


@dataclass(frozen=True, slots=True)
class SiteResult:
    """Outcome of testing one URL from inside a container."""

    url: str
    ok: bool
    duration_ms: int
    http_code: str
    reason: str
    exit_code: int


def probe_site(
    client: DockerClient,
    container: str,
    url: str,
    timeout: int,
) -> SiteResult:
    """Probe ``url`` with ``wget --spider`` from inside ``container``'s netns."""
    cmd = ["wget", "--spider", "-S", f"--timeout={timeout}", "--tries=1", "-q", url]
    start = time.monotonic()
    try:
        result = client.exec_run(container, cmd)
        exit_code, output = result.exit_code, result.output
    except Exception as exc:  # exec itself failed (container gone, daemon error)
        duration_ms = int((time.monotonic() - start) * 1000)
        return SiteResult(url, False, duration_ms, "N/A", f"exec failed: {exc}", -1)

    duration_ms = int((time.monotonic() - start) * 1000)
    http_code = _parse_http_code(output)

    if exit_code == 0:
        return SiteResult(url, True, duration_ms, http_code, f"HTTP {http_code}", 0)
    if exit_code in WGET_PASS_CODES:
        # Site answered with an error (auth/4xx/5xx) — the tunnel is still up.
        reason = f"HTTP {http_code} (VPN working)"
        return SiteResult(url, True, duration_ms, http_code, reason, exit_code)
    return SiteResult(
        url, False, duration_ms, http_code, decode_wget_exit_code(exit_code), exit_code
    )
