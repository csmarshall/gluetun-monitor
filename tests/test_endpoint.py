"""Gluetun endpoint-log parsing, including the issue #17 apostrophe regression.

Why: the reported public IP/country/city/server are read from gluetun's own logs
(Tenet 2 — reflect the tunnel, not the host). The parse must be quote-safe: a
region like Provence-Alpes-Cote-d'Azur crashed the v1 monitor (#17), and a parse
that throws here would take down the whole loop.
"""

from __future__ import annotations

from gluetun_monitor.endpoint import EndpointInfo, get_endpoint_info, parse_endpoint

from .fakes import FakeDockerClient, make_inspect

_IP_GETTER = (
    "2026-05-10T12:31:08+02:00 INFO [ip getter] Public IP address is 31.40.215.70 "
    "(Switzerland, Zurich, Zürich - source: ipinfo)"
)
_WG = "2026-05-10T12:30:00+02:00 INFO [wireguard] Connecting to 185.156.46.20:51820"


def test_parse_basic_endpoint() -> None:
    """The happy path: IP, country, city, and WG server are all extracted."""
    info = parse_endpoint(f"{_WG}\n{_IP_GETTER}\n")
    assert info.public_ip == "31.40.215.70"
    assert info.country == "Switzerland"
    assert info.city == "Zurich"
    assert info.wg_server == "185.156.46.20"


def test_parse_apostrophe_location_issue_17() -> None:
    """The #17 regression: an apostrophe in the location must parse cleanly and
    never crash (this exact string broke the v1 xargs-based parse)."""
    line = (
        "2026-05-10T12:31:08+02:00 INFO [ip getter] Public IP address is 159.26.112.51 "
        "(France, Provence-Alpes-Cote-d'Azur, Marseille - "
        "source: ifconfig.co+ip2location+cloudflare)"
    )
    info = parse_endpoint(line)
    assert info.public_ip == "159.26.112.51"
    assert info.country == "France"
    assert info.city == "Provence-Alpes-Cote-d'Azur"


def test_parse_strips_control_chars_from_location() -> None:
    """The geo string comes from a third-party IP-getter, so control chars (CR,
    tab, ESC, bell …) must be stripped before they reach the log — neutralizing
    terminal-control injection — while legitimate Unicode (Zürich) is preserved.
    (Any leftover *printable* bytes are harmless without the control char.)"""
    # Use non-line-break controls (tab, ESC, bell): \r\n etc. are consumed by
    # splitlines() before parsing, so they can't reach the field anyway.
    line = (
        "INFO [ip getter] Public IP address is 5.6.7.8 "
        "(Switzer\tland, Z\x1b\x07ürich - source: x)"
    )
    info = parse_endpoint(line)
    assert info.country == "Switzerland"  # tab stripped
    assert info.city == "Zürich"          # ESC + bell stripped, Unicode kept
    assert all(ch.isprintable() for ch in info.country + info.city)


def test_parse_takes_last_ip_getter_line() -> None:
    """Logs accumulate; we report the *most recent* endpoint, not a stale one."""
    older = "INFO [ip getter] Public IP address is 1.1.1.1 (A, B, C - source: x)"
    newer = "INFO [ip getter] Public IP address is 2.2.2.2 (D, E, F - source: y)"
    info = parse_endpoint(f"{older}\n{newer}")
    assert info.public_ip == "2.2.2.2"
    assert info.country == "D"


def test_parse_missing_data_is_unknown() -> None:
    """No matching log lines → all-"unknown", not an exception (logging must be
    best-effort; it never gates the loop)."""
    info = parse_endpoint("nothing useful here\n")
    assert info == EndpointInfo()  # all "unknown"


def test_format_message() -> None:
    """The ENDPOINT log line includes every field in the v1.x layout."""
    info = EndpointInfo("1.2.3.4", "US", "NYC", "9.9.9.9")
    msg = info.format("NEW", "After restart")
    assert "IP: 1.2.3.4" in msg
    assert "Country: US" in msg
    assert "City: NYC" in msg
    assert "VPN Server: 9.9.9.9" in msg
    assert "Reason: After restart" in msg


def test_get_endpoint_info_from_client() -> None:
    """End to end: fetch a container's logs via the client and parse them."""
    fake = FakeDockerClient()
    raw = make_inspect("gluetun", id="gid")
    raw["_logs"] = _IP_GETTER
    fake.add(raw)
    info = get_endpoint_info(fake, "gluetun")
    assert info.public_ip == "31.40.215.70"
