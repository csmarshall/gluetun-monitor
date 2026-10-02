"""app-checks.conf parsing (ADR-0018).

Why: the file format is the front door for this feature, and its one real risk
is the whitespace/regex split — a regex containing '|' alternation must never
collide with sites.conf's own '|' option separator (that's the whole reason this
isn't just another sites.conf role).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gluetun_monitor.app_checks import load_rules, load_rules_report, parse_line


def test_parse_line_basic() -> None:
    rule, warnings = parse_line("^sonarr-.*$ https://provider.example/index.html")
    assert warnings == []
    assert rule is not None
    assert rule.pattern_str == "^sonarr-.*$"
    assert rule.pattern.search("sonarr-1") is not None
    assert rule.pattern.search("radarr-1") is None
    assert rule.spec.url == "https://provider.example/index.html"
    assert rule.spec.timeout is None
    assert rule.spec.tries is None
    assert rule.spec.failures is None


def test_parse_line_alternation_regex_survives_the_split() -> None:
    """A '|' inside the regex (alternation) must not be mistaken for sites.conf's
    '|key=value' separator — it's collision-free because the split is on
    whitespace, not '|', and container names never contain whitespace."""
    rule, warnings = parse_line("^(radarr|lidarr)$ https://provider.example/x|timeout=15")
    assert warnings == []
    assert rule is not None
    assert rule.pattern_str == "^(radarr|lidarr)$"
    assert rule.pattern.search("radarr") is not None
    assert rule.pattern.search("lidarr") is not None
    assert rule.pattern.search("sonarr") is None
    assert rule.spec.url == "https://provider.example/x"
    assert rule.spec.timeout == 15


def test_parse_line_failures_option() -> None:
    rule, warnings = parse_line("^x$ https://provider.example|failures=5")
    assert warnings == []
    assert rule is not None
    assert rule.spec.failures == 5


def test_parse_line_failures_option_capped() -> None:
    rule, warnings = parse_line("^x$ https://provider.example|failures=999")
    assert rule is not None
    assert rule.spec.failures is None  # over the cap -> ignored, not clamped
    assert any("exceeds the maximum" in w for w in warnings)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
    ],
)
def test_parse_line_blank(raw: str) -> None:
    assert parse_line(raw) == (None, [])


def test_parse_line_missing_url_is_rejected() -> None:
    rule, warnings = parse_line("^sonarr-.*$")
    assert rule is None
    assert warnings and "malformed" in warnings[0]


def test_parse_line_empty_url_is_rejected() -> None:
    """A regex followed by only options (no URL before the '|') has nothing to
    probe -- rest.split('|')[0] trims to empty, which parse_entry reports as
    no SiteSpec at all, not merely an unsafe one."""
    rule, warnings = parse_line("^x$ |timeout=5")
    assert rule is None
    assert warnings and "has no URL" in warnings[0]


def test_parse_line_invalid_regex_is_rejected() -> None:
    rule, warnings = parse_line("^sonarr-(unclosed https://provider.example")
    assert rule is None
    assert warnings and "invalid container regex" in warnings[0]


def test_parse_line_unsafe_url_is_rejected() -> None:
    """The same wget '--' guard / URL-safety checks sites.conf applies (Tenet 1) —
    a leading-dash URL could be parsed as a wget flag."""
    rule, warnings = parse_line("^x$ --directory-prefix=/etc")
    assert rule is None
    assert warnings and "Ignoring app-check rule" in warnings[0]


def test_parse_line_role_option_warns_and_is_ignored() -> None:
    """role= parses (it's the shared sites.conf grammar) but has no effect on an
    app-check rule -- an app-check rule always gates a restart by construction."""
    rule, warnings = parse_line("^x$ https://provider.example|role=advisory")
    assert rule is not None
    assert rule.spec.role == "advisory"  # parsed, but check_dependent_app never reads it
    assert any("has no effect on app-check rules" in w for w in warnings)


def test_load_rules_report_skips_blanks_and_comments(tmp_path: Path) -> None:
    conf = tmp_path / "app-checks.conf"
    conf.write_text(
        "# a comment\n"
        "\n"
        "   \n"
        "^sonarr-.*$   https://a.example\n"
        "  # indented comment\n"
        "^(radarr|lidarr)$    https://b.example|timeout=15\n"
    )
    rules, rejected = load_rules_report(conf)
    assert rejected == []
    assert [r.pattern_str for r in rules] == ["^sonarr-.*$", "^(radarr|lidarr)$"]


def test_load_rules_report_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Off by default: a missing APP_CHECKS_FILE contributes nothing, mirroring
    sites.load_specs_report's treatment of a missing sites.conf."""
    rules, rejected = load_rules_report(tmp_path / "does-not-exist.conf")
    assert rules == []
    assert rejected == []


def test_load_rules_report_duplicate_rule_is_rejected(tmp_path: Path) -> None:
    conf = tmp_path / "app-checks.conf"
    conf.write_text(
        "^x$ https://a.example\n"
        "^x$ https://a.example\n"
    )
    rules, rejected = load_rules_report(conf)
    assert len(rules) == 1
    assert any("duplicate rule" in reason for _entry, reason in rejected)


def test_load_rules_drops_warnings(tmp_path: Path) -> None:
    conf = tmp_path / "app-checks.conf"
    conf.write_text("^x$ https://a.example\n")
    assert [r.pattern_str for r in load_rules(conf)] == ["^x$"]


def test_load_rules_report_bad_line_does_not_block_good_ones(tmp_path: Path) -> None:
    conf = tmp_path / "app-checks.conf"
    conf.write_text(
        "^sonarr-.*$ https://a.example\n"
        "not-a-valid-line-no-url\n"
        "^radarr-.*$ https://b.example\n"
    )
    rules, rejected = load_rules_report(conf)
    assert [r.pattern_str for r in rules] == ["^sonarr-.*$", "^radarr-.*$"]
    assert len(rejected) == 1
