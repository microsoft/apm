"""Network host helpers shared by the install and adapter layers."""

import ipaddress

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def parse_host_address(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the IP address for *host*, or None when it is not an IP literal.

    Handles dotted IPv4/IPv6, bracket-stripped IPv6, trailing-dot forms,
    and decimal or hexadecimal IPv4 integer encodings that defeat a naive
    ``ipaddress.ip_address(hostname)`` check.
    """
    if not host:
        return None
    normalized = host.strip().rstrip(".")
    if not normalized:
        return None
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        if normalized.lower().startswith("0x"):
            base = 16
        elif normalized.isdigit():
            base = 10
        else:
            return None
        try:
            value = int(normalized, base)
        except ValueError:
            return None
        if not 0 <= value <= 0xFFFFFFFF:
            return None
        return ipaddress.ip_address(value)


def is_loopback_host(host: str | None) -> bool:
    """Return True when *host* names a loopback address.

    Covers the conventional loopback hostnames (localhost, ip6-localhost,
    ip6-loopback) plus loopback IP literals (127.0.0.0/8, ::1), including
    decimal-encoded forms.
    """
    if not host:
        return False
    lowered = host.lower()
    if lowered in _LOOPBACK_HOSTNAMES:
        return True
    addr = parse_host_address(lowered)
    return addr is not None and addr.is_loopback
