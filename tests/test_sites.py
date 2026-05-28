"""sites.conf parsing, trim (incl. issue #17), and URL classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from gluetun_monitor.sites import (
    hostname_of,
    ip_pool,
    is_ip_literal,
    parse_sites_conf,
    resolvable_pool,
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
    assert trim(raw) == expected


def test_parse_skips_comments_blanks_and_whitespace(tmp_path: Path) -> None:
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


def test_parse_missing_file_raises(tmp_path: Path) -> None:
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
    assert hostname_of(url) == host


@pytest.mark.parametrize(
    ("host", "expected"),
    [("1.1.1.1", True), ("2606:4700:4700::1111", True),
     ("www.google.com", False), ("", False)],
)
def test_is_ip_literal(host: str, expected: bool) -> None:
    assert is_ip_literal(host) is expected


def test_pools_split_resolvable_from_ip_literals() -> None:
    sites = ["https://www.google.com", "https://1.1.1.1", "https://cloudflare.com"]
    assert resolvable_pool(sites) == ["https://www.google.com", "https://cloudflare.com"]
    assert ip_pool(sites) == ["https://1.1.1.1"]
