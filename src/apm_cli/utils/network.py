"""Network-host classification utilities.

Canonical owner of the local/private-host detection decision used to
determine whether non-HTTPS URLs are acceptable (loopback, link-local,
RFC 1918 private ranges, cloud-metadata IPs) versus must be rejected
(genuinely remote hosts that require HTTPS for security).

Route all ``is_local_or_metadata_host`` decisions through this module.
Do not re-implement or duplicate this logic in call sites.
"""

from __future__ import annotations

import ipaddress

__all__ = ["is_local_or_metadata_host"]


def is_local_or_metadata_host(host: str | None) -> bool:
    """Return True for loopback, link-local, RFC 1918, or cloud-metadata IPs.

    Used to decide whether a non-HTTPS URL is acceptable for a local dev
    workflow (loopback, private network) vs. must be rejected for a genuinely
    remote host.  The check covers:

    - Named loopback aliases: ``localhost``, ``ip6-localhost``, ``ip6-loopback``
    - Numeric loopback: ``127.x.x.x``, ``::1``
    - Link-local: ``169.254.x.x``, ``fe80::/10``
    - RFC 1918 private ranges: ``10.x``, ``172.16-31.x``, ``192.168.x``
    - Cloud-metadata IPs: ``169.254.169.254`` (AWS/Azure/GCP)
    - Multicast and unspecified addresses
    - Decimal-encoded loopback obfuscations (e.g. ``2130706433`` == ``127.0.0.1``)
    """
    if not host:
        return False
    lowered = host.lower()
    if lowered in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        # urlparse keeps decimal-encoded forms like '2130706433' (== 127.0.0.1)
        # as the hostname string; try int parse to catch that obfuscation.
        try:
            addr = ipaddress.ip_address(int(lowered))
        except (ValueError, TypeError):
            return False
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_unspecified
    )
