"""Gluetun endpoint-log parsing, including the issue #17 apostrophe regression."""

from __future__ import annotations

from gluetun_monitor.endpoint import EndpointInfo, get_endpoint_info, parse_endpoint

from .fakes import FakeDockerClient, make_inspect

_IP_GETTER = (
    "2026-05-10T12:31:08+02:00 INFO [ip getter] Public IP address is 31.40.215.70 "
    "(Switzerland, Zurich, Zürich - source: ipinfo)"
)
_WG = "2026-05-10T12:30:00+02:00 INFO [wireguard] Connecting to 185.156.46.20:51820"


def test_parse_basic_endpoint() -> None:
    info = parse_endpoint(f"{_WG}\n{_IP_GETTER}\n")
    assert info.public_ip == "31.40.215.70"
    assert info.country == "Switzerland"
    assert info.city == "Zurich"
    assert info.wg_server == "185.156.46.20"


def test_parse_apostrophe_location_issue_17() -> None:
    line = (
        "2026-05-10T12:31:08+02:00 INFO [ip getter] Public IP address is 159.26.112.51 "
        "(France, Provence-Alpes-Cote-d'Azur, Marseille - "
        "source: ifconfig.co+ip2location+cloudflare)"
    )
    info = parse_endpoint(line)
    assert info.public_ip == "159.26.112.51"
    assert info.country == "France"
    assert info.city == "Provence-Alpes-Cote-d'Azur"


def test_parse_takes_last_ip_getter_line() -> None:
    older = "INFO [ip getter] Public IP address is 1.1.1.1 (A, B, C - source: x)"
    newer = "INFO [ip getter] Public IP address is 2.2.2.2 (D, E, F - source: y)"
    info = parse_endpoint(f"{older}\n{newer}")
    assert info.public_ip == "2.2.2.2"
    assert info.country == "D"


def test_parse_missing_data_is_unknown() -> None:
    info = parse_endpoint("nothing useful here\n")
    assert info == EndpointInfo()  # all "unknown"


def test_format_message() -> None:
    info = EndpointInfo("1.2.3.4", "US", "NYC", "9.9.9.9")
    msg = info.format("NEW", "After restart")
    assert "IP: 1.2.3.4" in msg
    assert "Country: US" in msg
    assert "City: NYC" in msg
    assert "VPN Server: 9.9.9.9" in msg
    assert "Reason: After restart" in msg


def test_get_endpoint_info_from_client() -> None:
    fake = FakeDockerClient()
    raw = make_inspect("gluetun", id="gid")
    raw["_logs"] = _IP_GETTER
    fake.add(raw)
    info = get_endpoint_info(fake, "gluetun")
    assert info.public_ip == "31.40.215.70"
