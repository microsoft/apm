"""Credential-resolution helpers extracted from ``core.token_manager``.

Extracted to keep ``GitHubTokenManager`` under 400 LOC.
Functions here are called from thin wrapper static/class-methods on
``GitHubTokenManager``; they should not normally be imported directly.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from urllib.parse import urlparse

from apm_cli.utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    is_github_hostname,
    is_valid_fqdn,
)

logger = logging.getLogger(__name__)

# `git credential fill` may invoke OS credential helpers that show
# interactive dialogs (e.g. Windows Credential Manager account picker).
# The 60s default prevents false negatives on slow helpers.
DEFAULT_CREDENTIAL_TIMEOUT = 60
MAX_CREDENTIAL_TIMEOUT = 180


def _format_credential_host(host: str, port: int | None) -> str:
    """Embed a custom port into the git credential ``host`` field.

    Per ``gitcredentials(7)``, there is no standalone ``port=`` attribute in
    the credential protocol -- port must be embedded into the host field as
    ``host:port``. Sending a separate ``port=`` line is silently ignored by
    helpers, collapsing two different services into one credential entry.

    Uses ``is not None`` (not truthy) so that ``None`` is the only sentinel
    for "no port", matching the rest of the port-handling logic.
    """
    return f"{host}:{port}" if port is not None else host


def _sanitize_credential_path(path: str) -> str:
    """Strip leading ``/``, reject control chars, allowlist URL schemes.

    The git credential protocol is line-oriented: a stray newline in the
    ``path`` value would let an attacker inject arbitrary attribute lines
    (``\\nusername=...`` etc.) into the credential request. Even though
    ``path`` originates from a parsed dependency reference (already
    constrained to URL components), we defensively reject any value that
    contains control characters or whitespace, returning an empty string
    so the caller skips the ``path=`` line entirely. This preserves the
    pre-disambiguation request rather than ever sending a malformed one.

    We also guard against accidental full-URL inputs (``https://...``).
    Today every caller passes ``owner/repo``, but if a future caller ever
    passes a full URL the naive ``lstrip('/')`` would yield
    ``https:/host/owner/repo`` which GCM silently ignores. Detect this
    via ``urlparse`` and use the URL's path component instead.

    Schemes are allowlisted to ``https``/``http``/``ssh`` (and the
    schemeless owner/repo case). ``urlparse`` is greedy about consuming
    embedded characters in non-hierarchical schemes (notably ``data:``
    and ``file:``), which would let those URI families bypass the
    char-scan -- the ``parsed.path`` after such schemes can still embed
    bytes the scan would otherwise reject. Reject anything off-allowlist.
    """
    parsed = urlparse(path)
    scheme = parsed.scheme.lower()
    if scheme and scheme not in ("https", "http", "ssh"):
        return ""
    cleaned = parsed.path.lstrip("/") if scheme else path.lstrip("/")
    if not cleaned:
        return ""
    for ch in cleaned:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace():
            return ""
    return cleaned


def _is_valid_credential_token(token: str) -> bool:
    """Validate that a credential-fill token looks like a real credential.

    Rejects garbage values that can appear when GIT_ASKPASS or credential
    helpers return prompt text instead of actual tokens.
    """
    if not token:
        return False
    if len(token) > 1024:
        return False
    if any(c in token for c in (" ", "\t", "\n", "\r")):
        return False
    prompt_fragments = ("Password for", "Username for", "password for", "username for")
    if any(fragment in token for fragment in prompt_fragments):  # noqa: SIM103
        return False
    return True


def _supports_gh_cli_host(host: str | None) -> bool:
    """Return True when *host* should use gh CLI fallback."""
    if not host:
        return False
    if is_github_hostname(host):
        return True

    configured_host = default_host().lower()
    host_lower = host.lower()
    if host_lower != configured_host:
        return False
    if configured_host == "github.com" or configured_host.endswith(".ghe.com"):
        return False
    if is_azure_devops_hostname(configured_host):
        return False
    return is_valid_fqdn(configured_host)


def _get_credential_timeout() -> int:
    """Return timeout (seconds) for ``git credential fill``.

    Configurable via ``APM_GIT_CREDENTIAL_TIMEOUT`` (1-180).
    """
    raw = os.environ.get("APM_GIT_CREDENTIAL_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_CREDENTIAL_TIMEOUT
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_CREDENTIAL_TIMEOUT
    return max(1, min(val, MAX_CREDENTIAL_TIMEOUT))


def resolve_credential_from_git(
    host: str, port: int | None = None, path: str | None = None
) -> str | None:
    """Resolve a credential from the git credential store.

    Uses `git credential fill` to query the user's configured credential
    helpers (macOS Keychain, Windows Credential Manager, gh CLI, etc.).
    This is the same mechanism git clone uses internally.

    Args:
        host: The git host to resolve credentials for (e.g., "github.com")
        port: Optional non-standard git port (e.g. 7999 for Bitbucket DC).
            Embedded into the ``host`` field per ``gitcredentials(7)`` --
            a standalone ``port=`` line is not part of the protocol.
        path: Optional repository path (``org/repo``). When provided,
            a ``path=`` line is appended to the credential request so
            helpers configured with ``credential.useHttpPath = true``
            (notably Git Credential Manager for multi-account users)
            can disambiguate the target URL and pick the right
            stored account without prompting.

    Returns:
        The password/token from the credential store, or None if unavailable
    """
    host_field = _format_credential_host(host, port)
    stdin_lines = ["protocol=https", f"host={host_field}"]
    if path:
        sanitized = _sanitize_credential_path(path)
        if sanitized:
            stdin_lines.append(f"path={sanitized}")
    stdin = "\n".join(stdin_lines) + "\n\n"
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_get_credential_timeout(),
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "" if sys.platform != "win32" else "echo",
            },
        )
        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            if line.startswith("password="):
                token = line[len("password=") :]
                if token and _is_valid_credential_token(token):
                    return token
                return None
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def resolve_credential_from_gh_cli(host: str | None) -> str | None:
    """Resolve a token from the active gh CLI account for *host*.

    Uses ``gh auth token --hostname <host>`` as a non-interactive fallback
    before invoking OS credential helpers that may display UI.

    Eligibility is gated by :func:`_supports_gh_cli_host` so all callers
    share one path: hosts the gh CLI does not support (None/empty, ADO,
    unrelated FQDNs) return ``None`` immediately without spawning a
    subprocess. A non-zero exit, invalid output, missing ``gh`` binary,
    or timeout all return ``None``; ``stderr`` is debug-logged on
    non-zero exit so ``--verbose`` users can see why the call missed.
    """
    if not _supports_gh_cli_host(host):
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", host],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_get_credential_timeout(),
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1",
            },
        )
        if result.returncode != 0:
            logger.debug(
                "gh auth token failed for %s: %s",
                host,
                (result.stderr or "").strip()[:200],
            )
            return None

        token = result.stdout.strip()
        if token and _is_valid_credential_token(token):
            return token
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("gh auth token errored for %s: %s", host, exc)
        return None
