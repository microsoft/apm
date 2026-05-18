"""Marketplace add-command helpers."""

from __future__ import annotations

import builtins
import re
import sys
import urllib.parse as _up

from ...core.command_logger import CommandLogger
from ...utils.path_security import validate_path_segments

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_TRUSTED_MARKETPLACE_HOST_KINDS = ("github", "ghe_cloud", "ghes", "gitlab")


def _parse_https_url_segments(raw: str) -> tuple[str, list[str]]:
    """Parse an HTTPS marketplace URL; return ``(embedded_host, path_segments)``.

    Raises ``ValueError`` if the URL is missing a host.
    """
    parsed = _up.urlparse(raw)
    embedded_host = (parsed.hostname or "").strip().lower()
    if not embedded_host:
        raise ValueError(f"HTTPS URL is missing a host: '{raw}'")
    path = _up.unquote(parsed.path or "")
    if path.endswith(".git"):
        path = path[:-4]
    return embedded_host, [seg for seg in path.split("/") if seg]


def _parse_marketplace_repo(repo: str, host_flag: str | None) -> tuple[str, str, str | None]:
    """Parse a marketplace repo argument into ``(owner, repo_name, embedded_host)``.

    Accepted forms:
      * ``OWNER/REPO``                       (2 segments)
      * ``HOST/OWNER/REPO``                  (3 segments, first is FQDN)
      * ``HOST/group/sub/.../REPO``          (N>=4 segments, first is FQDN -- GHES nested paths)
      * ``OWNER/group/sub/.../REPO``         (N>=3 segments, first is NOT a FQDN)
      * ``https://HOST/owner/.../repo[.git]`` (full HTTPS URL)
      * ``http://HOST/owner/.../repo[.git]``  (full HTTP URL -- rejected with explicit error)

    Returns ``(owner, repo_name, embedded_host)`` where ``embedded_host`` is the
    host carried by the input itself (``HOST/...`` shorthand or HTTPS URL host)
    or ``None`` for bare ``OWNER/REPO`` shorthand.

    Raises ``ValueError`` on malformed input. The caller is responsible for
    enforcing the trusted-host allowlist on the returned ``embedded_host``.

    The returned segments are validated through ``validate_path_segments`` to
    reject path-traversal sequences (``..``, ``.``, ``~``).
    """
    from ...utils.github_host import is_valid_fqdn

    raw = (repo or "").strip()
    if not raw:
        raise ValueError("Empty repository argument")

    if any(ord(c) < 32 for c in raw):
        raise ValueError("Repository argument contains invalid control characters")

    embedded_host: str | None = None
    lowered = raw.lower()

    if lowered.startswith("http://"):
        # Reject HTTP at parse time. APM does not ship an --allow-insecure
        # escape hatch for marketplace add: a MITM adversary on an HTTP fetch
        # of marketplace.json could inject attacker-controlled plugin source
        # URLs, with no audit trail.
        raise ValueError(
            f"Insecure HTTP URL rejected: '{raw}'. Use HTTPS for marketplace registration."
        )

    if lowered.startswith("https://"):
        embedded_host, segments = _parse_https_url_segments(raw)
    else:
        # Mirror the HTTPS branch: decode percent-encoded sequences before splitting
        # so '%2E%2E' becomes '..' and is caught by validate_path_segments below.
        raw_decoded = _up.unquote(raw)
        segments = [seg for seg in raw_decoded.split("/") if seg]

    if len(segments) < 2:
        raise ValueError(
            f"Invalid format: '{raw}'. "
            f"Expected 'OWNER/REPO', 'HOST/OWNER/REPO', or a full HTTPS URL."
        )

    if embedded_host is None and is_valid_fqdn(segments[0]):
        # Shorthand carries an explicit host (e.g. 'gitlab.com/org/repo').
        if len(segments) < 3:
            raise ValueError(
                f"Invalid format: '{raw}'. When the first segment is a host FQDN, "
                f"at least 'HOST/OWNER/REPO' is required."
            )
        embedded_host = segments[0].lower()
        segments = segments[1:]

    repo_name = segments[-1]
    owner_segments = segments[:-1]
    if not owner_segments or not repo_name:
        raise ValueError(f"Invalid format: '{raw}'. Expected 'OWNER/REPO'.")

    # Reject conflicting --host BEFORE security validation so the user gets the
    # clearest possible error.
    if embedded_host and host_flag and host_flag.strip().lower() != embedded_host:
        # shlex.quote prevents shell-metacharacter injection in the
        # copy-paste suggestion (round-4 supply-chain nit).
        import shlex as _shlex

        raise ValueError(
            f"Conflicting host: --host '{host_flag}' does not match "
            f"'{embedded_host}' in '{raw}'.\n"
            f"To fix: drop --host and run: apm marketplace add {_shlex.quote(raw)}"
        )

    # validate_path_segments rejects '.', '..', '~' and cross-platform backslash
    # variants in any single segment. Validate the joined owner path and the
    # repo name independently so the error messages are precise.
    owner_path = "/".join(owner_segments)
    validate_path_segments(owner_path, context="marketplace owner path", reject_empty=True)
    validate_path_segments(repo_name, context="marketplace repo name", reject_empty=True)

    return owner_path, repo_name, embedded_host


def _marketplace_add_unsupported_host_error(
    resolved_host: str,
    quoted_repo: str,
    quoted_host: str,
    host_kind: str,
) -> str:
    """User-facing error when ``apm marketplace add`` rejects the resolved host.

    *quoted_repo* and *quoted_host* must already be ``shlex.quote``-safe for shell
    copy-paste (see call sites).
    """
    if host_kind == "ado":
        return (
            f"Host '{resolved_host}' is not supported for marketplace registration.\n"
            "APM marketplaces must be hosted on GitHub, GitHub Enterprise, or GitLab."
        )
    return (
        f"Host '{resolved_host}' is not supported.\n"
        "Supported marketplace hosts: github.com, *.ghe.com, "
        "GitHub Enterprise Server (configure GITHUB_HOST), "
        "and GitLab (gitlab.com or self-managed via GITLAB_HOST or APM_GITLAB_HOSTS).\n\n"
        "To use GitHub Enterprise Server on this host:\n"
        f"  export GITHUB_HOST={quoted_host}\n"
        "Then re-run:\n"
        f"  apm marketplace add {quoted_repo}\n\n"
        "To use self-managed GitLab on this host:\n"
        f"  export GITLAB_HOST={quoted_host}\n"
        "(or list the host in APM_GITLAB_HOSTS for multiple instances.)\n"
        "Then re-run:\n"
        f"  apm marketplace add {quoted_repo}\n"
    )


def _is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and _ALIAS_PATTERN.match(value) is not None


def _resolve_host_for_add(
    host: str | None, embedded_host: str | None, logger: CommandLogger
) -> str:
    """Resolve the effective git host; exits on invalid FQDN."""
    from ...utils.github_host import default_host, is_valid_fqdn

    if host is not None:
        normalized_host = host.strip().lower()
        if not is_valid_fqdn(normalized_host):
            logger.error(
                f"Invalid host: '{host}'. Expected a valid host FQDN (for example, 'github.com').",
                symbol="error",
            )
            sys.exit(1)
        return normalized_host
    if embedded_host is not None:
        return embedded_host
    return default_host()


def _check_trusted_host(resolved_host: str, repo: str, logger: CommandLogger) -> None:
    """Exit with an error when resolved_host is not a trusted marketplace host."""
    from ...core.auth import AuthResolver

    host_info = AuthResolver.classify_host(resolved_host)
    if host_info.kind not in _TRUSTED_MARKETPLACE_HOST_KINDS:
        import shlex as _shlex

        quoted_repo = _shlex.quote(repo)
        quoted_host = _shlex.quote(resolved_host)
        logger.error(
            _marketplace_add_unsupported_host_error(
                resolved_host, quoted_repo, quoted_host, host_info.kind
            )
        )
        sys.exit(1)


def _resolve_display_name_for_add(
    name: str | None, manifest_name: str, repo_name: str, logger: CommandLogger
) -> tuple[str, str]:
    """Return (display_name, alias_source) for marketplace registration."""
    if name is not None:
        return name, "--name flag"
    if manifest_name and _is_valid_alias(manifest_name):
        return manifest_name, f"manifest.name ('{manifest_name}')"
    display_name = repo_name
    if manifest_name and not _is_valid_alias(manifest_name):
        logger.warning(
            f"Manifest declares name '{manifest_name}' which is not a "
            f"valid alias (must match [a-zA-Z0-9._-]+). "
            f"Falling back to repo name.",
            symbol="warning",
        )
        alias_source = f"repo name (manifest.name '{manifest_name}' invalid)"
    else:
        alias_source = "repo name (manifest.name missing)"
    return display_name, alias_source
