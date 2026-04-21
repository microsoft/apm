"""MCP install-time, non-blocking safety warnings (F5 SSRF + F7 shell metachars).

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. These checks fire during ``apm install --mcp`` to surface
likely-misconfiguration to the user without blocking the operation.

Categories:

- **F5 (SSRF)**: warn when a self-defined remote MCP URL points at
  internal/metadata addresses (loopback, link-local, RFC1918, cloud IMDS).
- **F7 (Shell metachars)**: warn when env values OR the stdio ``command``
  field contain shell metacharacters. MCP stdio servers spawn via
  ``execve``-style calls (no shell), so metacharacters are passed
  literally rather than evaluated -- a confused user expecting shell
  semantics will get surprising behavior.

These are deliberately *warnings*, not errors, because legitimate paths
exist (e.g. a private homelab MCP server bound to a loopback address).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable, Optional


# F7: tokens that would be evaluated by a real shell but are NOT evaluated
# when an MCP stdio server runs through ``execve``-style spawning.
_SHELL_METACHAR_TOKENS = ("$(", "`", ";", "&&", "||", "|", ">>", ">", "<")

# F5: well-known cloud metadata endpoints surfaced as constants for
# explicit allow/deny review.
_METADATA_HOSTS = {
    "169.254.169.254",   # AWS / Azure / GCP IMDS
    "100.100.100.200",   # Alibaba Cloud
    "fd00:ec2::254",     # AWS IPv6 IMDS
}


def _is_internal_or_metadata_host(host: str) -> bool:
    """Return True when ``host`` resolves/parses to an internal IP.

    Covers cloud metadata IPs, loopback, link-local, and RFC1918 ranges.
    Defensive against ``ValueError``/``OSError`` from name resolution.
    """
    if not host:
        return False
    if host in _METADATA_HOSTS:
        return True
    candidates: list = [host]
    # Strip brackets from IPv6 literals.
    bare = host.strip("[]")
    if bare != host:
        candidates.append(bare)
    # Resolve hostname when it is not already an IP literal.
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        try:
            resolved = socket.gethostbyname(bare)
            candidates.append(resolved)
        except (OSError, UnicodeError):
            pass
    for c in candidates:
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_private:
            return True
        if c in _METADATA_HOSTS:
            return True
    return False


def warn_ssrf_url(url: Optional[str], logger) -> None:
    """F5: warn (do not block) when URL points at an internal/metadata host."""
    if not url:
        return
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return
    if _is_internal_or_metadata_host(host):
        logger.warning(
            f"URL '{url}' points to an internal or metadata address; "
            f"verify intent before installing."
        )


def warn_shell_metachars(env, logger, command: Optional[str] = None) -> None:
    """F7: warn (do not block) on shell metacharacters in env values or stdio command.

    MCP stdio servers spawn via ``execve``-style calls with no shell, so
    these characters are passed literally rather than evaluated. Users who
    think they are setting ``FOO=$(secret)`` will be surprised.

    Also covers ``command`` itself (e.g. ``command: "npx|curl evil.com"``)
    which would otherwise pass the whitespace-rejection guard but still
    indicate a confused user expecting shell evaluation.
    """
    if env:
        for key, value in env.items():
            sval = "" if value is None else str(value)
            for tok in _SHELL_METACHAR_TOKENS:
                if tok in sval:
                    logger.warning(
                        f"Env value for '{key}' contains shell metacharacter "
                        f"'{tok}'; reminder these are NOT shell-evaluated."
                    )
                    break
    if command and isinstance(command, str):
        for tok in _SHELL_METACHAR_TOKENS:
            if tok in command:
                logger.warning(
                    f"'command' contains shell metacharacter '{tok}'; "
                    f"reminder MCP stdio servers run via execve (no shell). "
                    f"This will be passed literally."
                )
                break
