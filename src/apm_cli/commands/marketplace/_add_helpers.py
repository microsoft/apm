"""Marketplace add-command helpers."""

from __future__ import annotations

import builtins
import json
import re
import sys
import traceback
from pathlib import Path

import click
import yaml

from ...core.command_logger import CommandLogger
from ...marketplace.builder import BuildOptions, BuildReport, MarketplaceBuilder, ResolvedPackage
from ...marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import (
    ConfigSource,
    detect_config_source,
    load_marketplace_config,
    migrate_marketplace_yml,
)
from ...marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ...marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)
from ...marketplace.ref_resolver import RefResolver, RemoteRef
from ...marketplace.semver import SemVer, parse_semver, satisfies_range
from ...marketplace.yml_schema import load_marketplace_yml
from ...utils.console import _rich_info, _rich_warning  # noqa: F401
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console, _is_interactive

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_TRUSTED_MARKETPLACE_HOST_KINDS = ("github", "ghe_cloud", "ghes", "gitlab")


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
    from urllib.parse import urlparse

    from ...utils.github_host import is_valid_fqdn

    raw = (repo or "").strip()
    if not raw:
        raise ValueError("Empty repository argument")

    # Reject control characters and percent-encoded traversal. urlparse normalizes
    # the path but does not unescape; we unescape eagerly so the security guards
    # below see the real bytes the user typed.
    import urllib.parse as _up

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
        parsed = urlparse(raw)
        embedded_host = (parsed.hostname or "").strip().lower()
        if not embedded_host:
            raise ValueError(f"HTTPS URL is missing a host: '{raw}'")
        # urlparse leaves the path percent-encoded; decode for segment splitting
        # so traversal markers like '%2E%2E' are caught by validate_path_segments.
        path = _up.unquote(parsed.path or "")
        if path.endswith(".git"):
            path = path[:-4]
        segments = [seg for seg in path.split("/") if seg]
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
