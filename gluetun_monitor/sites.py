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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Integer per-URL option keys understood after the ``|`` separator. Both map onto
# existing ``probe_site`` params, so honoring them needs no probe rework.
_INT_OPTION_KEYS = ("timeout", "tries")

# Per-URL role (#110): governs whether a site's failure gates a gluetun restart.
# ``critical`` (the default) restarts as before; ``advisory`` is still probed and
# recorded for reachability but NEVER triggers a restart — for a site that can't be
# reached through any exit (geo-blocked/anti-VPN) yet should stay observed.
DEFAULT_ROLE = "critical"
_VALID_ROLES = ("critical", "advisory")

# All option keys, for the "unknown option" warning.
_OPTION_KEYS = (*_INT_OPTION_KEYS, "role")

# Sanity ceilings for per-URL overrides (#77). The Docker transport read-timeout
# is sized from the worst-case probe (see ``worst_case_probe_seconds``), so an
# absurd override would silently stretch every loop and the transport with it —
# a probe stuck for minutes stalls ALL site checks (they fan out, but the loop
# joins them). 300s tolerates genuinely slow-but-alive sites; anything beyond
# is a config mistake, warned about and skipped per the forgiving+loud contract.
MAX_URL_TIMEOUT = 300
MAX_URL_TRIES = 5
_OPTION_CAPS = {"timeout": MAX_URL_TIMEOUT, "tries": MAX_URL_TRIES}


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
    # #110: "critical" (default) gates a restart on failure; "advisory" never does.
    role: str = DEFAULT_ROLE


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
    role = DEFAULT_ROLE
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
        # Role is a string enum, not an integer knob — handle it before the int path.
        if key == "role":
            if value.lower() not in _VALID_ROLES:
                warnings.append(
                    f"unknown role {value!r} (known: {', '.join(_VALID_ROLES)}) — "
                    f"treating as {DEFAULT_ROLE}"
                )
                continue
            role = value.lower()
            continue
        if key not in _OPTION_KEYS:
            warnings.append(f"unknown option {key!r} (known: {', '.join(_OPTION_KEYS)})")
            continue
        # isascii() first: str.isdigit() is True for Unicode digits (e.g. '²')
        # that int() refuses, so isdigit() alone lets the int() below raise —
        # turning a config typo into a crash instead of a warn-and-skip (#73).
        if not (value.isascii() and value.isdigit()) or int(value) < 1:
            warnings.append(f"invalid {key} {value!r} (want a positive integer)")
            continue
        if int(value) > _OPTION_CAPS[key]:
            warnings.append(
                f"{key}={value} exceeds the maximum {_OPTION_CAPS[key]} (#77) — "
                f"ignoring the override"
            )
            continue
        values[key] = int(value)
    return (
        SiteSpec(url, timeout=values.get("timeout"), tries=values.get("tries"), role=role),
        warnings,
    )


def worst_case_probe_seconds(
    specs: list[SiteSpec], default_timeout: int, default_tries: int
) -> int:
    """Worst-case single-probe runtime across ``specs``: the max of each site's
    *effective* ``timeout*tries`` (its overrides, else the globals).

    This is what the Docker transport read-timeout must comfortably exceed (#77):
    ``wget --spider`` emits nothing while it waits, so an exec that legitimately
    runs long looks idle to the transport — if the transport gives up first, a
    slow *success* is misreported as a probe failure, and a per-URL override
    meant to stop restarts guarantees them instead.
    """
    worst = default_timeout * max(1, default_tries)
    for s in specs:
        timeout = s.timeout if s.timeout is not None else default_timeout
        tries = s.tries if s.tries is not None else default_tries
        worst = max(worst, timeout * max(1, tries))
    return worst


def trim(s: str) -> str:
    """Strip leading/trailing whitespace.

    Pure replacement for the v1.x bash ``trim`` (which avoided ``xargs`` so that
    quotes in input — e.g. ``Provence-Alpes-Cote-d'Azur`` — don't crash; issue
    #17). Quotes are preserved.
    """
    return s.strip()


def strip_inline_comment(line: str) -> str:
    """Drop an inline ``#`` comment: a ``#`` preceded by whitespace starts a
    comment (``https://a.example  # note`` → ``https://a.example``); a ``#``
    with no whitespace before it is URL content (a fragment) and is kept.

    v2 extension (v1 skipped whole-line comments only): without it the trailing
    comment becomes part of the URL — a phantom site that fails every poll and
    counts toward FAIL_THRESHOLD (#73).
    """
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def parse_sites_conf(path: str | Path) -> list[str]:
    """Read a sites.conf and return the list of test URLs.

    Skips blank/whitespace-only lines and ``#`` comments (whole-line and
    inline — see :func:`strip_inline_comment`); trims each entry. Raises
    FileNotFoundError if the file is missing (caller decides severity).
    """
    sites: list[str] = []
    text = Path(path).read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        site = trim(strip_inline_comment(raw_line))
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
    entry with no host component tests nothing. Embedded whitespace never
    belongs in a URL — it means something leaked past parsing (e.g. an inline
    comment in a ``SITES`` env entry, which unlike the file is not
    comment-stripped) and would only ever poll as a phantom failure (#73).
    """
    if site.startswith("-"):
        return "looks like a command-line flag (leading '-')"
    if any(ch.isspace() for ch in site):
        return "contains whitespace (inline comment or malformed entry?)"
    # Reject embedded credentials (userinfo): a connectivity probe needs no auth,
    # and the full URL — password included — would be logged verbatim and reflected
    # into notifications (#86). Rejecting keeps it out of the monitored set entirely.
    parsed = urlsplit(site if "://" in site else f"http://{site}")
    if parsed.username or parsed.password:
        return "contains embedded credentials (user:pass@…) — remove them"
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


def warn_rejects(warn: Callable[[str], None], rejected: Iterable[tuple[str, str]]) -> None:
    """Emit one WARN per rejected ``(entry, reason)`` from :func:`load_specs_report`.

    Shared by the startup preflight and the live-reload path so a bad ``sites.conf``
    entry (an unsafe URL dropped, or a per-URL option warned-and-defaulted) is
    surfaced identically whenever the file is parsed — not loudly at startup but
    silently on a live edit.
    """
    for entry, reason in rejected:
        warn(f"Ignoring unsafe site entry {entry!r}: {reason}")


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
