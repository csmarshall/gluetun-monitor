"""Parse gluetun's own logs for the active VPN endpoint (IP / location / server).

Mirrors v1.x ``log_endpoint_info``. The location parse is deliberately
quote-safe (issue #17): a region like ``Provence-Alpes-Cote-d'Azur`` must parse
cleanly and never crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .docker_client import DockerClient

_PUBLIC_IP_RE = re.compile(r"Public IP address is ([0-9.]+)")
_LOCATION_RE = re.compile(r"\(([^)]+)\)")
_WG_SERVER_RE = re.compile(r"Connecting to ([0-9.]+)")


@dataclass(frozen=True, slots=True)
class EndpointInfo:
    """The VPN endpoint as reported by gluetun's logs."""

    public_ip: str = "unknown"
    country: str = "unknown"
    city: str = "unknown"
    wg_server: str = "unknown"

    def format(self, status: str, reason: str = "") -> str:
        """Render the v1.x ENDPOINT log message body."""
        return (
            f"Status: {status} | IP: {self.public_ip} | Country: {self.country} | "
            f"City: {self.city} | VPN Server: {self.wg_server} | Reason: {reason}"
        )


def _last_match(pattern: re.Pattern[str], lines: list[str]) -> str | None:
    """The capture group of the last line matching ``pattern``, or None."""
    for line in reversed(lines):
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def parse_endpoint(logs: str) -> EndpointInfo:
    """Extract endpoint info from raw gluetun log text."""
    ip_getter_lines = [
        ln for ln in logs.splitlines() if "ip getter" in ln.lower() and "Public IP address" in ln
    ]
    wireguard_lines = [
        ln for ln in logs.splitlines() if "wireguard" in ln.lower() and "Connecting to" in ln
    ]

    public_ip = "unknown"
    country = "unknown"
    city = "unknown"

    if ip_getter_lines:
        line = ip_getter_lines[-1]
        ip_match = _PUBLIC_IP_RE.search(line)
        if ip_match:
            public_ip = ip_match.group(1)
        loc_match = _LOCATION_RE.search(line)
        if loc_match:
            # Drop the trailing " - source: ..." annotation, then split fields.
            location = loc_match.group(1).split(" - source:")[0]
            fields = [f.strip() for f in location.split(",")]
            if fields and fields[0]:
                country = fields[0]
            if len(fields) > 1 and fields[1]:
                city = fields[1]

    wg_server = _last_match(_WG_SERVER_RE, wireguard_lines) or "unknown"

    return EndpointInfo(public_ip=public_ip, country=country, city=city, wg_server=wg_server)


def get_endpoint_info(client: DockerClient, container: str) -> EndpointInfo:
    """Fetch and parse the current endpoint from ``container``'s logs."""
    try:
        logs = client.logs(container)
    except Exception:
        return EndpointInfo()
    return parse_endpoint(logs)
