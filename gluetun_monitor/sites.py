"""sites.conf parsing and URL classification.

Parsing mirrors v1.x exactly: skip blank lines and ``#`` comments, trim
whitespace. Classification (resolvable hostname vs IP-literal) feeds the
ADR-0006 per-dependent viability pool — only hostname URLs exercise a
dependent's DNS.

**Per-URL tunables (#60).** An entry may carry optional ``|key=value`` overrides
after the URL — ``https://slow.example|timeout=25|tries=2`` — to widen the probe
timeout/retries for a *specific* slow-but-alive site without touching the global
``TIMEOUT``/``WGET_TRIES``. ``|`` is the separator because it is URL-excluded
(RFC 3986 — a real URL never contains a bare one, so it can't collide with URL
content) *and* survives the comma-splitting of the ``SITES`` env CSV, so one
syntax works identically in the file and the env. A bare URL (no ``|``) parses
exactly as before. Parsing is forgiving + loud: an unknown key or bad value is
warned about and skipped, never fatal — the URL is still monitored on the
global defaults.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Per-URL option keys understood after the ``|`` separator. Both map onto
# existing ``probe_site`` params, so honoring them needs no probe rework.
_OPTION_KEYS = ("timeout", "tries")


@dataclass(frozen=True, slots=True)
class SiteSpec:
    """A test URL plus any per-URL probe overrides.

    ``timeout``/``tries`` are ``None`` when the entry carried no override, meaning
    "inherit the global ``TIMEOUT``/``WGET_TRIES``" — so an un-annotated site
    behaves exactly as it did before #60. A set value is the effective per-request
    knob for *this* URL only.
    """

    url: str
    timeout: int | None = None
    tries: int | None = None


def parse_entry(raw: str) -> tuple[SiteSpec | None, list[str]]:
    """Parse one ``url[|key=value...]`` entry into a :class:`SiteSpec` + warnings.

    Splits on ``|``: the first field is the URL, the rest are ``key=value``
    options. Returns ``(None, [])`` for an empty entry (caller skips it like a
    blank line). Forgiving + loud: a malformed option, unknown key, or
    non-positive-integer value yields a human-readable warning and is skipped —
    the URL is still returned on the global defaults rather than dropped over a
    typo (Tenet 1). Warnings are surfaced once at startup by the report loaders.
    """
    parts = raw.split("|")
    url = trim(parts[0])
    if not url:
        return None, []
    values: dict[str, int] = {}
    warnings: list[str] = []
    for opt in parts[1:]:
        opt = trim(opt)
        if not opt:
            continue
        if "=" not in opt:
            warnings.append(f"malformed option {opt!r} (expected key=value)")
            continue
        key, _, value = opt.partition("=")
        key, value = trim(key).lower(), trim(value)
        if key not in _OPTION_KEYS:
            warnings.append(f"unknown option {key!r} (known: {', '.join(_OPTION_KEYS)})")
            continue
        if not value.isdigit() or int(value) < 1:
            warnings.append(f"invalid {key} {value!r} (want a positive integer)")
            continue
        values[key] = int(value)
    return SiteSpec(url, timeout=values.get("timeout"), tries=values.get("tries")), warnings


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


def unsafe_site_reason(site: str) -> str | None:
    """Why ``site`` is unsafe/useless to probe, or None if it's acceptable.

    A leading-dash entry would be parsed by ``wget``/``ping`` as an *option*
    rather than a URL — e.g. ``--directory-prefix=/etc`` could write files inside
    a container (the exec layer also guards this with a ``--`` separator; this
    keeps such a bogus "flag URL" out of the test set entirely — Tenet 1). An
    entry with no host component tests nothing.
    """
    if site.startswith("-"):
        return "looks like a command-line flag (leading '-')"
    if not hostname_of(site):
        return "no host component"
    return None


def load_specs_report(
    config_file: str | Path, sites_env: str | None
) -> tuple[list[SiteSpec], list[tuple[str, str]]]:
    """Effective :class:`SiteSpec` list + rejected ``(entry, reason)`` pairs.

    The union of ``sites.conf`` and the ``SITES`` env CSV, each entry parsed for
    per-URL ``|`` options (:func:`parse_entry`). Deduplicated by URL, first
    occurrence wins (file entries first, then env-only), so a URL repeated with
    different options keeps the first set. ``rejected`` carries both unsafe-URL
    drops (see :func:`unsafe_site_reason`) and per-URL option warnings, so the
    startup path can surface them all loudly in one place.
    """
    try:
        file_entries = parse_sites_conf(config_file)
    except FileNotFoundError:
        file_entries = []
    env_entries = parse_sites_csv(sites_env) if sites_env else []

    seen: set[str] = set()
    specs: list[SiteSpec] = []
    rejected: list[tuple[str, str]] = []
    for raw in (*file_entries, *env_entries):
        spec, warnings = parse_entry(raw)
        if spec is None or spec.url in seen:
            continue
        seen.add(spec.url)
        reason = unsafe_site_reason(spec.url)
        if reason is not None:
            rejected.append((spec.url, reason))
            continue  # an unsafe URL is dropped; its options are moot
        rejected.extend((spec.url, w) for w in warnings)
        specs.append(spec)
    return specs, rejected


def load_specs(config_file: str | Path, sites_env: str | None) -> list[SiteSpec]:
    """Effective :class:`SiteSpec` list (URLs + per-URL overrides), warnings dropped.

    The file is re-read on each call, so editing it (including its ``|`` options)
    is picked up live; the env value is fixed at process start.
    """
    return load_specs_report(config_file, sites_env)[0]


def load_sites_report(
    config_file: str | Path, sites_env: str | None
) -> tuple[list[str], list[tuple[str, str]]]:
    """Like :func:`load_sites`, but also returns rejected ``(entry, reason)`` pairs.

    Used at startup so the operator gets a loud warning about dropped entries and
    bad per-URL options; the per-loop path uses :func:`load_specs` directly.
    """
    specs, rejected = load_specs_report(config_file, sites_env)
    return [s.url for s in specs], rejected


def load_sites(config_file: str | Path, sites_env: str | None) -> list[str]:
    """Effective test URL set: the union of ``sites.conf`` and the ``SITES`` env CSV.

    Either source is optional; both may be supplied. Duplicates are removed,
    first-occurrence order preserved (file entries first, then env-only ones).
    Unsafe entries (see :func:`unsafe_site_reason`) are dropped. A missing config
    file contributes nothing (not an error here — the caller decides whether the
    *combined* set being empty is fatal). The file is re-read on each call, so
    editing it is picked up live; the env value is fixed at process start.
    """
    return [s.url for s in load_specs(config_file, sites_env)]


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
