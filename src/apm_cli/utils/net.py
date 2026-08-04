"""Network host helpers shared by the install and adapter layers."""

import ipaddress

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def parse_host_address(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the IP address for *host*, or None when it is not an IP literal.

    urlparse keeps decimal-encoded forms like '2130706433' (== 127.0.0.1) as
    the hostname string, so an int parse is attempted to catch that
    obfuscation.
    """
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        try:
            return ipaddress.ip_address(int(host))
        except (ValueError, TypeError):
            return None


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
