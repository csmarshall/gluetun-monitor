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


def parse_sites_csv(value: str) -> list[str]:
    """Parse a comma-separated ``SITES`` env value into trimmed URLs."""
    return [s for s in (trim(x) for x in value.split(",")) if s]


def load_sites(config_file: str | Path, sites_env: str | None) -> list[str]:
    """Effective test set: the union of ``sites.conf`` and the ``SITES`` env CSV.

    Either source is optional; both may be supplied. Duplicates are removed,
    first-occurrence order preserved (file entries first, then env-only ones). A
    missing config file contributes nothing (not an error here — the caller
    decides whether the *combined* set being empty is fatal). The file is re-read
    on each call, so editing it is picked up live; the env value is fixed at
    process start.
    """
    try:
        file_sites = parse_sites_conf(config_file)
    except FileNotFoundError:
        file_sites = []
    env_sites = parse_sites_csv(sites_env) if sites_env else []

    seen: set[str] = set()
    merged: list[str] = []
    for site in (*file_sites, *env_sites):
        if site not in seen:
            seen.add(site)
            merged.append(site)
    return merged


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
