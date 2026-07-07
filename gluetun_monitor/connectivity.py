"""Connectivity probing — ``wget --spider`` from inside a container's netns.

This is the ADR-0001 authoritative test. The question it answers is "did traffic
that's supposed to traverse the tunnel reach a server?" — so **any HTTP response
(even 401/403/404/5xx) is a pass**: it proves DNS resolved, the connection went
through the tunnel, and a server answered (Tenet 3 — a broken tunnel is not a sad
website). Only a genuine failure to get *any* HTTP response (DNS failure,
connection refused, TLS error, timeout) counts as a connectivity failure.

Keying on "did we get an HTTP status?" rather than on wget's exit code is also
what makes this correct across wget implementations: gluetun ships **GNU wget**
(HTTP errors → exit 6/8), but dependent containers commonly run **busybox wget**
(linuxserver/Alpine images), which returns **exit 1 for any HTTP error response**.
The old exit-code-only map (0/6/8 = pass) silently misclassified a busybox
dependent's harmless 404 as a failure. The exit code is now only a fallback for
the case where no HTTP status was emitted at all.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .docker_client import DockerClient

# wget exit codes where the site *responded* — used only as a fallback when no
# HTTP status line was captured (GNU: 0 success, 6 auth, 8 HTTP 4xx/5xx). Note
# busybox wget does NOT follow this scheme (it returns 1 for HTTP errors), which
# is exactly why HTTP-response detection takes priority over the exit code.
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
# Lines wget (GNU or busybox) emits describing an actual failure, newest-last.
_ERROR_HINTS = ("wget:", "failed:", "unable to resolve", "bad address",
                "connection refused", "timed out", "tls", "ssl", "name or service")
# Substrings that specifically indicate a DNS *resolution* failure (GNU + busybox
# + musl/glibc resolver wording). This is the one per-container fault a dependent
# viability probe uniquely detects (ADR-0006): dependents share gluetun's netns,
# so their L3/L4 egress is gluetun's (already proven) — only their own resolver
# can differ.
_DNS_FAIL_HINTS = ("bad address", "unable to resolve", "name or service not known",
                   "temporary failure in name resolution", "could not resolve",
                   "name does not resolve", "no address associated")


def _is_dns_failure(output: str) -> bool:
    """True if wget's output positively indicates a DNS resolution failure."""
    low = output.lower()
    return any(hint in low for hint in _DNS_FAIL_HINTS)


def decode_wget_exit_code(code: int) -> str:
    """Human-readable reason for a wget exit code (v1.x ``decode_wget_exit_code``)."""
    return _WGET_EXIT_MESSAGES.get(code, f"Unknown error (code {code})")


def _parse_http_code(output: str) -> str:
    """Last HTTP status code seen in ``wget -S`` output, or 'N/A'."""
    matches = _HTTP_CODE_RE.findall(output)
    return matches[-1] if matches else "N/A"


def _strip_controls(value: str) -> str:
    """Drop non-printable chars (C0/C1 controls, ANSI escapes, RTL/bidi overrides)
    from externally-controlled text before it enters a log record — a dependent's
    wget output could otherwise inject escape sequences that spoof log lines (#86).
    Printable Unicode (e.g. ``Zürich``) is kept. Mirrors ``endpoint._clean``.
    """
    return "".join(ch for ch in value if ch.isprintable())


def _extract_error(output: str, exit_code: int) -> str:
    """The real failure reason: wget's own last diagnostic line if we can find it,
    otherwise the decoded exit code. Avoids the useless bare 'Generic error'.
    """
    for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
        low = line.lower()
        if any(hint in low for hint in _ERROR_HINTS):
            # Trim a leading "wget: " for readability; sanitize the dependent-
            # controlled remainder before it reaches a log line (#86).
            reason = line[len("wget:"):].strip() if low.startswith("wget:") else line
            return _strip_controls(reason)
    return decode_wget_exit_code(exit_code)  # a static internal string — safe


@dataclass(frozen=True, slots=True)
class SiteResult:
    """Outcome of testing one URL from inside a container.

    ``ok`` is the strict "did the site respond" signal used by gluetun's root test
    (tunnel up?). ``dns_failed`` is the narrower "this container's resolver failed"
    signal used by dependent viability (the only per-container fault — see module
    docstring). They are distinct on purpose: a connect-refused/timeout makes
    ``ok`` False but ``dns_failed`` False (the dependent's DNS still worked).
    """

    url: str
    ok: bool
    duration_ms: int
    http_code: str
    reason: str
    exit_code: int
    dns_failed: bool = False


def probe_site(
    client: DockerClient,
    container: str,
    url: str,
    timeout: int,
    tries: int = 1,
) -> SiteResult:
    """Probe ``url`` with ``wget --spider`` from inside ``container``'s netns.

    ``timeout`` (TIMEOUT) and ``tries`` (WGET_TRIES) are the standardized per-request
    knobs, applied identically to gluetun's GNU wget and the dependents' busybox
    wget. No ``-q``: we want wget's diagnostics in the output so a real failure
    reports *why* (not a bare exit code). Classification is HTTP-response-first (see
    the module docstring), so it's correct for both GNU and busybox wget.
    """
    # ``--`` ends option parsing: a site like "--directory-prefix=/etc" must be
    # treated as a (bad) URL, never as a wget flag that could write files inside
    # the container. sites.py already rejects leading-dash entries; this is the
    # belt-and-suspenders second layer (Tenet 1).
    cmd = ["wget", "--spider", "-S", f"--timeout={timeout}", f"--tries={tries}", "--", url]
    start = time.monotonic()
    try:
        result = client.exec_run(container, cmd)
        exit_code, output = result.exit_code, result.output
    except Exception as exc:  # exec itself failed (container gone, daemon error)
        duration_ms = int((time.monotonic() - start) * 1000)
        return SiteResult(url, False, duration_ms, "N/A", f"exec failed: {exc}", -1)

    duration_ms = int((time.monotonic() - start) * 1000)
    http_code = _parse_http_code(output)

    # Primary signal: any HTTP response proves resolve + connect + egress worked,
    # whatever the status code or wget implementation's exit convention.
    if http_code != "N/A":
        suffix = "" if exit_code == 0 else " (responded)"
        return SiteResult(url, True, duration_ms, http_code, f"HTTP {http_code}{suffix}", exit_code)
    # No HTTP status seen. Fall back to the GNU exit-code map for the rare case
    # wget signals "responded" without a parseable header...
    if exit_code in WGET_PASS_CODES:
        reason = f"responded (exit {exit_code})"
        return SiteResult(url, True, duration_ms, http_code, reason, exit_code)
    # ...otherwise it's a genuine no-response failure — report the real reason and
    # flag whether it was specifically a DNS resolution failure (the dependent
    # viability layer acts only on that; gluetun's root test acts on `ok`).
    return SiteResult(
        url, False, duration_ms, http_code, _extract_error(output, exit_code), exit_code,
        dns_failed=_is_dns_failure(output),
    )
