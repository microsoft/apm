"""SSRF + shell-metacharacter advisory warnings for MCP install.

Extracted from ``apm_cli.commands.install`` to keep the CLI module under the
architectural LOC budget (see ``tests/unit/install/test_architecture_invariants``).
The helpers here emit non-blocking warnings that help users spot surprising
behaviour without refusing the install:

* :func:`warn_ssrf_url` (F5) -- flag URLs pointing at cloud-metadata/internal
  IPs so an accidental mis-configured registry or remote server is visible
  rather than silent.
* :func:`warn_shell_metachars` (F7) -- remind users that MCP stdio servers
  spawn via ``execve`` with no shell, so ``$(...)``/backticks/pipes in
  ``env`` values (and, when provided, the ``command`` string) are passed
  literally rather than evaluated.

These functions are pure advisories -- they never raise, never block, and
always route messages through the caller's logger so they flow through the
standard ``CommandLogger`` / ``DiagnosticCollector`` pipeline.
"""

from __future__ import annotations

# F7 shell-expansion residue scan: tokens a real shell would evaluate but
# which the ``execve``-style spawn of an MCP stdio server will NOT evaluate.
_SHELL_METACHAR_TOKENS = ("$(", "`", ";", "&&", "||", "|", ">>", ">", "<")

# F5 SSRF: well-known cloud metadata endpoints, surfaced as constants so
# allow/deny review stays explicit.
_METADATA_HOSTS = {
    "169.254.169.254",   # AWS / Azure / GCP IMDS
    "100.100.100.200",   # Alibaba Cloud
    "fd00:ec2::254",     # AWS IPv6 IMDS
}


def _is_internal_or_metadata_host(host: str) -> bool:
    """Return True when ``host`` parses/resolves to an internal IP.

    Covers cloud metadata IPs, loopback, link-local, and RFC1918 ranges.
    Defensive against ``ValueError``/``OSError`` from name resolution.
    """
    import ipaddress
    import socket

    if not host:
        return False
    if host in _METADATA_HOSTS:
        return True
    candidates: list = [host]
    bare = host.strip("[]")
    if bare != host:
        candidates.append(bare)
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


def warn_ssrf_url(url, logger):
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


def warn_shell_metachars(env, logger):
    """F7: warn (do not block) when env values contain shell metacharacters.

    MCP stdio servers spawn via ``execve``-style calls with no shell, so
    these characters are passed literally rather than evaluated.  Users
    who think they are setting ``FOO=$(secret)`` will be surprised.
    """
    if not env:
        return
    for key, value in env.items():
        sval = "" if value is None else str(value)
        for tok in _SHELL_METACHAR_TOKENS:
            if tok in sval:
                logger.warning(
                    f"Env value for '{key}' contains shell metacharacter "
                    f"'{tok}'; reminder these are NOT shell-evaluated."
                )
                break
