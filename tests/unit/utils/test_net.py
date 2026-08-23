"""Unit coverage for canonical network host parsing."""

import ipaddress

import pytest

from apm_cli.utils.net import is_loopback_host, parse_host_address


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", ipaddress.IPv4Address("127.0.0.1")),
        ("::1", ipaddress.IPv6Address("::1")),
        ("2130706433", ipaddress.IPv4Address("127.0.0.1")),
        ("0x7f000001", ipaddress.IPv4Address("127.0.0.1")),
        ("127.0.0.1.", ipaddress.IPv4Address("127.0.0.1")),
        ("example.com", None),
        (None, None),
    ],
)
def test_parse_host_address(
    host: str | None,
    expected: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
) -> None:
    assert parse_host_address(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "ip6-localhost",
        "ip6-loopback",
        "LOCALHOST.",
        "127.0.0.1",
        "127.0.0.2",
        "127.255.255.254",
        "::1",
        "127.0.0.1.",
    ],
)
def test_loopback_hosts_detected(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "example.com",
        "192.168.1.10",
        "10.0.0.1",
        "169.254.169.254",
        "8.8.8.8",
        "0.0.0.0",  # noqa: S104 - test input for unspecified-address rejection.
        "224.0.0.1",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
        "127.1",
        "localhost..",
        "::ffff:127.0.0.1",
        "281472812449793",
    ],
)
def test_non_loopback_hosts_rejected(host: str | None) -> None:
    assert is_loopback_host(host) is False
