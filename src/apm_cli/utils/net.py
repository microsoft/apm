"""Network host helpers shared by the install and adapter layers."""

import ipaddress

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_IPV6_LOOPBACK = ipaddress.IPv6Address("::1")


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
    ip6-loopback) plus unambiguous loopback IP literals (127.0.0.0/8,
    ::1). A single DNS root dot and case differences are normalized for the
    conventional hostnames. Ambiguous integer IPv4 encodings are rejected.
    """
    if not host:
        return False
    lowered = host.strip().lower().removesuffix(".")
    if lowered in _LOOPBACK_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return (isinstance(addr, ipaddress.IPv4Address) and addr.is_loopback) or (
        isinstance(addr, ipaddress.IPv6Address) and addr == _IPV6_LOOPBACK
    )


_RFC1918_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


def is_local_network_host(host: str | None) -> bool:
    """Return True for a loopback host or an RFC 1918 private IPv4 literal.

    The one decision for which ``http://`` hosts count as a local network:
    loopback names and addresses (:func:`is_loopback_host`) plus 10.0.0.0/8,
    172.16.0.0/12 and 192.168.0.0/16. Hostnames other than the loopback names
    are never local, since DNS can point them anywhere.
    """
    if is_loopback_host(host):
        return True
    if not host:
        return False
    # Strict literal parsing only: integer, hex and leading-zero forms are
    # read differently by socket resolvers, so they never count as private.
    try:
        addr = ipaddress.ip_address(host.strip().removesuffix("."))
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv4Address) and any(
        addr in network for network in _RFC1918_NETWORKS
    )
