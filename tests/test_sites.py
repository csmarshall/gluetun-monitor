"""sites.conf parsing, trim (incl. issue #17), and URL classification.

Why: parsing is the front door for the test targets, and the resolvable-vs-IP
classification decides whether a dependent's DNS gets exercised (ADR-0006). trim
carries a real regression (#17): the old xargs-based trim crashed on quotes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gluetun_monitor.sites import (
    hostname_of,
    ip_pool,
    is_ip_literal,
    parse_sites_conf,
    resolvable_pool,
    strip_inline_comment,
    trim,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("   hello world   ", "hello world"),
        ("\t  hello\t", "hello"),
        ("", ""),
        ("     ", ""),
        (" say \"hi\" ", 'say "hi"'),
        # issue #17: xargs choked on this; trim must preserve the apostrophe.
        (" Provence-Alpes-Cote-d'Azur ", "Provence-Alpes-Cote-d'Azur"),
    ],
)
def test_trim(raw: str, expected: str) -> None:
    """trim strips surrounding whitespace but preserves quotes/apostrophes — the
    last case is the #17 regression that crashed the v1 xargs-based trim."""
    assert trim(raw) == expected


def test_parse_skips_comments_blanks_and_whitespace(tmp_path: Path) -> None:
    """Only real URLs survive parsing: comments (incl. indented), blank, and
    whitespace-only lines are dropped, and entries are trimmed."""
    conf = tmp_path / "sites.conf"
    conf.write_text(
        "# a comment\n"
        "\n"
        "   \n"
        "https://www.google.com\n"
        "  https://cloudflare.com  \n"
        "   # indented comment\n"
        "https://1.1.1.1\n"
    )
    assert parse_sites_conf(conf) == [
        "https://www.google.com",
        "https://cloudflare.com",
        "https://1.1.1.1",
    ]


def test_parse_strips_inline_comments_but_keeps_fragments(tmp_path: Path) -> None:
    """#73: a trailing ``# note`` must not become part of the URL (pre-fix it
    produced a phantom permanently-failing site that counted toward
    FAIL_THRESHOLD). A ``#`` with no whitespace before it is a URL fragment and
    is preserved."""
    conf = tmp_path / "sites.conf"
    conf.write_text(
        "https://a.example  # the slow one\n"
        "https://b.example\t# tab comment\n"
        "https://c.example#fragment\n"
    )
    assert parse_sites_conf(conf) == [
        "https://a.example",
        "https://b.example",
        "https://c.example#fragment",
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("https://a.example  # note", "https://a.example  "),
        ("# whole line", ""),
        ("   # indented", "   "),
        ("https://a.example#frag", "https://a.example#frag"),
        ("https://a.example", "https://a.example"),
        ("", ""),
    ],
)
def test_strip_inline_comment(line: str, expected: str) -> None:
    """The comment starts at a ``#`` that begins the line or follows whitespace;
    trailing whitespace is left for trim() (the callers always trim after)."""
    assert strip_inline_comment(line) == expected


def test_parse_missing_file_raises(tmp_path: Path) -> None:
    """A missing file raises FileNotFoundError — load_sites() relies on this to
    treat an absent file as "no contribution" rather than crashing."""
    with pytest.raises(FileNotFoundError):
        parse_sites_conf(tmp_path / "nope.conf")


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://www.google.com", "www.google.com"),
        ("https://1.1.1.1", "1.1.1.1"),
        ("http://example.com:8080/path", "example.com"),
        ("https://[2606:4700:4700::1111]/x", "2606:4700:4700::1111"),
        ("bare-host.example", "bare-host.example"),
    ],
)
def test_hostname_of(url: str, host: str) -> None:
    """Host extraction handles ports, paths, IPv6 brackets, and scheme-less input
    — the input to the IP-literal classification below."""
    assert hostname_of(url) == host


@pytest.mark.parametrize(
    ("host", "expected"),
    [("1.1.1.1", True), ("2606:4700:4700::1111", True),
     ("www.google.com", False), ("", False)],
)
def test_is_ip_literal(host: str, expected: bool) -> None:
    """IPv4/IPv6 literals are recognized as such; DNS names and empty are not —
    this is what separates DNS-exercising URLs from connectivity-only ones."""
    assert is_ip_literal(host) is expected


def test_pools_split_resolvable_from_ip_literals() -> None:
    """The resolvable pool (hostnames) drives dependent DNS testing; the IP pool
    is the connectivity-only fallback (ADR-0006)."""
    sites = ["https://www.google.com", "https://1.1.1.1", "https://cloudflare.com"]
    assert resolvable_pool(sites) == ["https://www.google.com", "https://cloudflare.com"]
    assert ip_pool(sites) == ["https://1.1.1.1"]
