"""app-checks.conf parsing — per-dependent app-level (HTTP status) checks (ADR-0018).

gluetun's root test (ADR-0001) and the per-dependent DNS/connectivity viability pool
(ADR-0006) both answer "does the tunnel work," and both deliberately treat *any* HTTP
response — even 401/403/5xx — as a pass (Tenet 3: a broken tunnel is not a sad
website). That leaves one case uncovered: a VPN provider whose *specific exit
endpoint* is L7-blocked by a destination site. The tunnel is up, DNS is fine, the site
answers — with a 403, because it fingerprinted the exit IP, not because anything is
broken. Those tests correctly call that healthy; it isn't, for a dependent that
specifically needs that site.

A rule pairs a container-name regex with a URL: checked serially, from inside every
currently-running dependent whose name matches, treating a 4xx/5xx (or no response at
all) as the failure signal — a deliberate, scoped narrowing of Tenet 3's "any response
is a pass," not a contradiction of it (that tenet governs "does the path work at all";
this governs "is this specific site blocking this specific exit," which a 403 answers
directly). This module owns only the file format and parsing; the check itself reuses
``connectivity.probe_site`` unchanged (see ``monitor.Monitor.check_dependent_app``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .sites import DEFAULT_ROLE, SiteSpec, parse_entry, strip_inline_comment, trim, unsafe_site_reason


@dataclass(frozen=True, slots=True)
class AppCheckRule:
    """One app-checks.conf line: a container-name regex plus the URL (and any
    ``|timeout=``/``|tries=``/``|failures=`` overrides) to probe from inside every
    currently-running dependent whose name matches ``pattern``.
    """

    pattern_str: str
    pattern: re.Pattern[str]
    spec: SiteSpec  # url, timeout/tries/failures overrides — role is unused here

    @property
    def key(self) -> str:
        """Stable identity for this rule's failure counter and no-match streak.

        Keyed on regex + URL together, not line position: a live-reload that only
        reorders unrelated lines must not reset an in-progress streak, but a rule
        whose regex OR url actually changed is a materially different rule and
        should start clean rather than inherit a stale one.
        """
        return f"{self.pattern_str} -> {self.spec.url}"


def parse_line(raw: str) -> tuple[AppCheckRule | None, list[str]]:
    """Parse one ``<container-name-regex><whitespace><url>[|timeout=N|tries=N|failures=N]``
    line into an :class:`AppCheckRule` + warnings.

    Splits on the *first* run of whitespace — collision-free with regex syntax
    (including a ``|`` alternation like ``^(radarr|lidarr)$``, which would collide
    with sites.conf's own ``|`` option separator) because Docker container names,
    and therefore any regex meant to match them, are restricted to
    ``[a-zA-Z0-9][a-zA-Z0-9_.-]+`` and so never contain a literal space. The
    remainder is ``sites.conf``'s own ``url[|timeout=N|tries=N|failures=N]``
    grammar, parsed by the same :func:`sites.parse_entry` (same forgiving-and-loud
    validation, same option caps). Returns ``(None, [])`` for an empty entry
    (caller skips it like a blank line); a malformed regex or URL yields ``(None,
    [reason])`` — a bad container-selector has no safe partial meaning, so the
    whole line is dropped rather than guessed at (Tenet 1).
    """
    line = trim(raw)
    if not line:
        return None, []
    parts = line.split(None, 1)
    if len(parts) < 2:
        return None, [f"malformed app-check line {raw!r} (expected '<regex> <url>')"]
    pattern_str, rest = parts
    try:
        pattern = re.compile(pattern_str)
    except re.error as exc:
        return None, [f"invalid container regex {pattern_str!r}: {exc}"]
    spec, warnings = parse_entry(rest)
    if spec is None:
        return None, [f"app-check rule {pattern_str!r} has no URL"]
    reason = unsafe_site_reason(spec.url)
    if reason is not None:
        return None, [f"Ignoring app-check rule {pattern_str!r}: {reason}"]
    if spec.role != DEFAULT_ROLE:
        # role has no meaning here (an app-check rule always gates a restart by
        # construction) — warn rather than silently accept a typo'd expectation.
        warnings = [*warnings, f"role={spec.role!r} has no effect on app-check rules (ignored)"]
    return AppCheckRule(pattern_str, pattern, spec), warnings


def load_rules_report(path: str | Path) -> tuple[list[AppCheckRule], list[tuple[str, str]]]:
    """Parsed rules + rejected ``(entry, reason)`` pairs from ``APP_CHECKS_FILE``.

    A missing file contributes nothing and is not an error — the feature is off by
    default (mirrors ``sites.load_specs_report``'s treatment of a missing
    ``sites.conf``). Re-read on every call, so a live edit is picked up the same
    way ``sites.conf`` is.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    rules: list[AppCheckRule] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = trim(strip_inline_comment(raw_line))
        if not line:
            continue
        rule, warnings = parse_line(line)
        if rule is None:
            rejected.extend((line, w) for w in warnings)
            continue
        if rule.key in seen:
            rejected.append((line, "duplicate rule (same regex + URL as an earlier line) — skipped"))
            continue
        seen.add(rule.key)
        rejected.extend((rule.pattern_str, w) for w in warnings)
        rules.append(rule)
    return rules, rejected


def load_rules(path: str | Path) -> list[AppCheckRule]:
    """Parsed rules only, warnings dropped — the per-loop path."""
    return load_rules_report(path)[0]
