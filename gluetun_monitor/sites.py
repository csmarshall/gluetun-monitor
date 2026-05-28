"""sites.conf parsing and URL classification.

Parsing mirrors v1.x exactly: skip blank lines and ``#`` comments, trim
whitespace. Classification (resolvable hostname vs IP-literal) feeds the
ADR-0006 per-dependent viability pool — only hostname URLs exercise a
dependent's DNS.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit


def trim(s: str) -> str:
    """Strip leading/trailing whitespace.

    Pure replacement for the v1.x bash ``trim`` (which avoided ``xargs`` so that
    quotes in input — e.g. ``Provence-Alpes-Cote-d'Azur`` — don't crash; issue
    #17). Quotes are preserved.
    """
    return s.strip()


def parse_sites_conf(path: str | Path) -> list[str]:
    """Read a sites.conf and return the list of test URLs.

    Skips blank/whitespace-only lines and ``#`` comments; trims each entry.
    Raises FileNotFoundError if the file is missing (caller decides severity).
    """
    sites: list[str] = []
    text = Path(path).read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        site = trim(raw_line)
        if not site:
            continue
        sites.append(site)
    return sites


def hostname_of(url: str) -> str:
    """Return the host portion of a URL (no port, brackets stripped for IPv6)."""
    # urlsplit needs a scheme to populate .hostname; assume http if bare.
    parsed = urlsplit(url if "://" in url else f"http://{url}")
    return parsed.hostname or ""


def is_ip_literal(host: str) -> bool:
    """True if ``host`` is a bare IPv4/IPv6 address rather than a DNS name."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolvable_pool(sites: list[str]) -> list[str]:
    """URLs whose host is a DNS name — these exercise a dependent's resolver."""
    return [s for s in sites if not is_ip_literal(hostname_of(s))]


def ip_pool(sites: list[str]) -> list[str]:
    """URLs whose host is an IP literal — connectivity-only fallback."""
    return [s for s in sites if is_ip_literal(hostname_of(s))]
