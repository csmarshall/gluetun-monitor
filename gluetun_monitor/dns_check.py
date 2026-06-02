"""Faithful, portable DNS-resolution validation inside a dependent container.

Because dependents share gluetun's netns (egress is gluetun's, already proven;
strands are caught upstream by the interface check), the only per-container fault
left to detect is the container's **own DNS resolution** (its `resolv.conf` /
`nsswitch.conf`). This module validates exactly that, with a cascade of
**getaddrinfo-faithful** tools — ones that resolve the way the application itself
does (libc `getaddrinfo` / nsswitch): ``wget`` (resolves before connecting),
``getent hosts``, and ``ping`` (resolves before pinging). We deliberately do
**not** use ``nslookup``/``dig``/``host``: those query DNS servers directly,
bypassing nsswitch/`/etc/hosts`/libc, so they can report "resolves" when the
app's `getaddrinfo` does not — a false-OK we must avoid.

Three outcomes, not two (Tenet 1 — don't guess):
- **OK** — a tool resolved the name.
- **BROKEN** — a tool ran and resolution positively failed (the fault we act on).
- **UNVALIDATED** — no usable tool exists in the container; we can't tell, so we
  don't guess. The caller logs it and falls back to the interface check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .connectivity import _is_dns_failure, probe_site
from .docker_client import DockerClient, ExecResult

# Exit codes / output that mean the tool itself isn't present in the container.
_ABSENT_EXIT_CODES = frozenset({-1, 126, 127})
_ABSENT_HINTS = ("executable file not found", "not found in $path", "no such file",
                 "applet not found")


class DnsStatus(Enum):
    """Outcome of a dependent DNS-resolution check."""

    OK = auto()
    BROKEN = auto()
    UNVALIDATED = auto()


@dataclass(frozen=True, slots=True)
class DnsResult:
    """A DNS check result: the verdict, the tool that produced it, and why."""

    status: DnsStatus
    tool: str
    reason: str


def _absent(result: ExecResult) -> bool:
    """True if the exec indicates the command isn't present in the container."""
    if result.exit_code in _ABSENT_EXIT_CODES:
        return True
    low = result.output.lower()
    return any(hint in low for hint in _ABSENT_HINTS)


def _try(client: DockerClient, container: str, cmd: list[str]) -> ExecResult | None:
    """Run ``cmd``; return its result, or None if the exec itself raised."""
    try:
        return client.exec_run(container, cmd)
    except Exception:
        return None


def validate_dns(
    client: DockerClient,
    container: str,
    url: str,
    host: str,
    timeout: int,
    tries: int = 1,
) -> DnsResult:
    """Validate that ``container`` can resolve ``host`` (the host part of ``url``).

    Cascades wget → getent → ping, stopping at the first tool that's actually
    present (gives OK or BROKEN). If none are present, returns UNVALIDATED.
    ``timeout``/``tries`` are the standardized per-request knobs (wget --timeout/
    --tries; ping -W).
    """
    # Tool 1 — wget (reuses the connectivity probe: it resolves via getaddrinfo
    # before connecting, so its result already tells us OK vs DNS-broken).
    # ``reason`` carries only the *detail*; the caller supplies the verb + verdict
    # ("reach ok/fail"), so we don't repeat "resolved"/"DNS FAILED" here.
    wget = probe_site(client, container, url, timeout, tries)
    if wget.exit_code not in _ABSENT_EXIT_CODES:
        if wget.dns_failed:
            return DnsResult(DnsStatus.BROKEN, "wget", wget.reason)
        if wget.http_code != "N/A":
            # Full round-trip: resolved DNS + TCP-connected + got an HTTP response.
            return DnsResult(DnsStatus.OK, "wget", f"HTTP {wget.http_code}")
        # DNS resolved but no HTTP response (connect refused / timeout / TLS).
        # Still viable (DNS is the only per-container fault), but say so honestly.
        return DnsResult(DnsStatus.OK, "wget", f"no HTTP: {wget.reason}")

    # Tool 2 — getent hosts (nsswitch-faithful; glibc images). 0 = resolved,
    # 2 = name not found. DNS-only (no connection attempt).
    getent = _try(client, container, ["getent", "hosts", "--", host])
    if getent is not None and not _absent(getent):
        if getent.exit_code == 0 and getent.output.strip():
            return DnsResult(DnsStatus.OK, "getent", "lookup only")
        if getent.exit_code == 2:
            return DnsResult(DnsStatus.BROKEN, "getent", "name not found")

    # Tool 3 — ping (resolves via getaddrinfo before sending; we only care that
    # resolution happened, not that ICMP succeeded — packets may be blocked).
    ping = _try(client, container, ["ping", "-c", "1", "-W", str(max(1, timeout)), "--", host])
    if ping is not None and not _absent(ping):
        if _is_dns_failure(ping.output):
            return DnsResult(DnsStatus.BROKEN, "ping", "resolution failed")
        low = ping.output.lower()
        resolved = (
            ping.exit_code == 0
            or "bytes from" in low
            or "transmitted" in low
            or "permission denied" in low  # resolved, then couldn't send (no CAP_NET_RAW)
            or "(" in ping.output  # "PING host (1.2.3.4)" — name resolved to an address
        )
        if resolved:
            return DnsResult(DnsStatus.OK, "ping", "lookup only")

    return DnsResult(DnsStatus.UNVALIDATED, "", "no resolver tool")
