"""Marketplace CLI package.

This package keeps click group wiring, shared helpers, and compatibility
exports for the marketplace command surface.
"""

from __future__ import annotations

import builtins
import logging
import re
import sys
from pathlib import Path

import click

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
from ...marketplace.ref_resolver import RefResolver, RemoteRef
from ...marketplace.semver import SemVer, parse_semver, satisfies_range
from ...marketplace.yml_schema import load_marketplace_yml
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console as _get_console
from .._helpers import _is_interactive as _is_interactive

logger = logging.getLogger(__name__)

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def _is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and _ALIAS_PATTERN.match(value) is not None


# ---------------------------------------------------------------------------
# Custom group for organised --help output
# ---------------------------------------------------------------------------


class MarketplaceGroup(click.Group):
    """Custom group that organises commands by audience."""

    _consumer_commands = [  # noqa: RUF012
        "add",
        "list",
        "browse",
        "update",
        "remove",
        "validate",
    ]
    _authoring_commands = [  # noqa: RUF012
        "init",
        "check",
        "outdated",
        "audit",
        "package",
        "migrate",
    ]

    def get_command(self, ctx, cmd_name):
        # The 'build' subcommand was removed in favour of the unified
        # 'apm pack' entrypoint. Surface a hard error with a migration
        # hint rather than silently aliasing.
        if cmd_name == "build":
            raise click.UsageError(
                "'apm marketplace build' was removed. Use 'apm pack' instead.\n"
                "marketplace.json is now produced by 'apm pack' when "
                "apm.yml has a 'marketplace:' block."
            )
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx, formatter):
        sections = [
            ("Consumer commands", self._consumer_commands),
            ("Authoring commands", self._authoring_commands),
        ]

        for section_name, cmd_names in sections:
            commands = []
            for name in cmd_names:
                cmd = self.get_command(ctx, name)
                if cmd is None:
                    continue
                if getattr(cmd, "hidden", False):
                    continue
                help_text = cmd.get_short_help_str(limit=150)
                commands.append((name, help_text))
            if commands:
                with formatter.section(section_name):
                    formatter.write_dl(commands)


def _load_yml_or_exit(logger):
    """Load ``./marketplace.yml`` from CWD or exit with an appropriate code.

    Returns the parsed ``MarketplaceYml`` on success.
    Calls ``sys.exit(1)`` on ``FileNotFoundError`` and
    ``sys.exit(2)`` on ``MarketplaceYmlError`` (schema/parse errors).
    """
    yml_path = Path.cwd() / "marketplace.yml"
    if not yml_path.exists():
        logger.error(
            "No marketplace.yml found. Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    try:
        return load_marketplace_yml(yml_path)
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)


def _load_config_or_exit(logger):
    """Load the marketplace config from CWD (apm.yml or marketplace.yml).

    Returns ``(project_root, config)``. Exits with code 1 when no config
    is found or both files coexist; exits with code 2 on validation errors.
    Emits a deprecation warning when the legacy file is in use.
    """
    project_root = Path.cwd()
    try:
        config = load_marketplace_config(
            project_root,
            warn_callback=lambda msg: logger.warning(msg, symbol="warning"),
        )
    except MarketplaceYmlError as exc:
        msg = str(exc)
        if msg.startswith("No marketplace config"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        if msg.startswith("Both apm.yml"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        logger.error(f"marketplace config error: {exc}", symbol="error")
        sys.exit(2)
    return project_root, config


def _warn_duplicate_names(logger, yml):
    """Emit a warning for each duplicate package name in *yml*."""
    seen: dict[str, int] = {}
    for idx, entry in enumerate(yml.packages):
        lower = entry.name.lower()
        if lower in seen:
            logger.warning(
                f"Duplicate package name '{entry.name}' "
                f"(packages[{seen[lower]}] and packages[{idx}]). "
                f"Consumers will see duplicate entries in browse.",
                symbol="warning",
            )
        else:
            seen[lower] = idx


def _find_duplicate_names(yml):
    """Return a diagnostic string if *yml* contains duplicate package names."""
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for idx, entry in enumerate(yml.packages):
        lower = entry.name.lower()
        if lower in seen:
            duplicates.append(f"'{entry.name}' (packages[{seen[lower]}] and packages[{idx}])")
        else:
            seen[lower] = idx
    if duplicates:
        return f"Duplicate names: {', '.join(duplicates)}"
    return ""


@click.group(cls=MarketplaceGroup, help="Manage marketplaces for discovery and governance")
@click.pass_context
def marketplace(ctx):
    """Register, browse, and search marketplaces."""


from .plugin import package  # noqa: E402

marketplace.add_command(package)


def _parse_marketplace_source(source: str, host_flag: str | None) -> tuple[str, str, str | None]:
    """Parse a marketplace source argument into ``(url, kind, embedded_host)``.

    Accepted forms (auto-detected, in order of test):

      * Local filesystem path -- absolute (``/...``), relative (``./...``,
        ``../...``), home (``~/...``), or Windows drive letter
        (``C:\\repos\\foo``). Returns ``kind="local"``, ``url`` is the
        ``file://`` URI form, ``embedded_host=None``.
      * ``file://`` URI -- same shape as a local path but explicit.
      * SCP-like SSH (``git@host:org/repo.git``). Returns ``kind`` derived
        from the host via ``AuthResolver.classify_host``, ``url`` rewritten
        to the SCP form (subprocess git understands it natively),
        ``embedded_host=<host>``.
      * Full HTTPS URL. Returns the URL untouched, ``embedded_host``
        extracted, ``kind`` classified by host. Hosts that ``AuthResolver``
        does not recognise as github/gitlab fall through as ``kind="git"``
        (subprocess git path).
      * ``OWNER/REPO`` or ``HOST/OWNER/.../REPO`` shorthand. The HOST
        segment is detected via ``is_valid_fqdn``. Returns a synthesised
        ``https://`` URL plus the resolved host.

    Raises ``ValueError`` on malformed input (single-segment, HTTP, empty,
    or path-traversal sequences in any segment).
    """
    from urllib.parse import urlparse

    from ...core.auth import AuthResolver
    from ...utils.github_host import is_valid_fqdn

    raw = (source or "").strip()
    if not raw:
        raise ValueError("Empty source argument")
    if any(ord(c) < 32 for c in raw):
        raise ValueError("Source argument contains invalid control characters")

    # --- Local path detection ---------------------------------------------
    # Catches absolute (/, ~), relative (./, ../), file:// URIs, and
    # Windows drive letters (C:\, C:/). Done BEFORE URL parsing so a path
    # like '/srv/foo' is not misread as a relative HTTP URL.
    if _looks_like_local_marketplace_source(raw):
        url = raw if raw.lower().startswith("file://") else f"file://{_expand_local_path(raw)}"
        return url, "local", None

    lowered = raw.lower()
    if lowered.startswith("http://"):
        raise ValueError(
            f"Insecure HTTP URL rejected: '{raw}'. Use HTTPS for marketplace registration."
        )

    # --- SCP-like SSH (git@host:org/repo.git) -----------------------------
    scp_match = _SCP_LIKE_RE.match(raw)
    if scp_match:
        host = scp_match.group("host").lower()
        path = scp_match.group("path")
        # Validate the path component does not carry traversal markers.
        for seg in (s for s in path.split("/") if s):
            validate_path_segments(seg, context="marketplace SSH path", reject_empty=True)
        host_info = AuthResolver.classify_host(host)
        kind = _host_kind_to_fetcher_kind(host_info.kind)
        return raw, kind, host

    # --- HTTPS URL --------------------------------------------------------
    if lowered.startswith("https://"):
        parsed = urlparse(raw)
        embedded_host = (parsed.hostname or "").strip().lower()
        if not embedded_host:
            raise ValueError(f"HTTPS URL is missing a host: '{raw}'")
        # Validate path segments for traversal markers.
        from urllib.parse import unquote as _unquote

        path_segments = [s for s in _unquote(parsed.path or "").split("/") if s]
        for seg in path_segments:
            validate_path_segments(seg, context="marketplace URL path", reject_empty=True)
        if not path_segments:
            raise ValueError(f"HTTPS URL is missing a repo path: '{raw}'")
        host_info = AuthResolver.classify_host(embedded_host)
        kind = _host_kind_to_fetcher_kind(host_info.kind)
        if kind in ("github", "gitlab") and len(path_segments) < 2:
            # GitHub / GitLab URLs are owner/repo-shaped; a single
            # path segment is ambiguous (no owner). Generic git URLs
            # (kind == "git") MAY legitimately have a single segment
            # (e.g. self-hosted ``https://gitea.example.com/repo``).
            raise ValueError(f"Invalid format: '{raw}'. Expected 'OWNER/REPO' in the URL path.")
        if host_flag and host_flag.strip().lower() != embedded_host:
            import shlex as _shlex

            raise ValueError(
                f"Conflicting host: --host '{host_flag}' does not match "
                f"'{embedded_host}' in '{raw}'.\n"
                f"To fix: drop --host and run: apm marketplace add {_shlex.quote(raw)}"
            )
        return raw, kind, embedded_host

    # --- Shorthand (OWNER/REPO or HOST/OWNER/.../REPO) --------------------
    from urllib.parse import unquote as _unquote

    raw_decoded = _unquote(raw)
    segments = [seg for seg in raw_decoded.split("/") if seg]
    if len(segments) < 2:
        raise ValueError(
            f"Invalid format: '{raw}'. "
            f"Expected 'OWNER/REPO', 'HOST/OWNER/REPO', a full HTTPS URL, "
            f"a local path, or an SSH URL."
        )

    embedded_host: str | None = None
    if is_valid_fqdn(segments[0]):
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

    owner_path = "/".join(owner_segments)
    validate_path_segments(owner_path, context="marketplace owner path", reject_empty=True)
    validate_path_segments(repo_name, context="marketplace repo name", reject_empty=True)

    if embedded_host and host_flag and host_flag.strip().lower() != embedded_host:
        import shlex as _shlex

        raise ValueError(
            f"Conflicting host: --host '{host_flag}' does not match "
            f"'{embedded_host}' in '{raw}'.\n"
            f"To fix: drop --host and run: apm marketplace add {_shlex.quote(raw)}"
        )

    from ...utils.github_host import default_host

    resolved_host = (host_flag or "").strip().lower() or embedded_host or default_host()
    host_info = AuthResolver.classify_host(resolved_host)
    kind = _host_kind_to_fetcher_kind(host_info.kind)
    url = f"https://{resolved_host}/{owner_path}/{repo_name}"
    return url, kind, resolved_host


# Backward-compat alias for any external callers.
_parse_marketplace_repo = _parse_marketplace_source


def _host_kind_to_fetcher_kind(host_kind: str) -> str:
    """Map ``AuthResolver.classify_host`` kinds to fetcher-table kinds.

    ``github`` / ``ghe_cloud`` / ``ghes`` -> ``"github"`` (Contents API)
    ``gitlab``                            -> ``"gitlab"`` (REST v4 raw)
    Everything else (``ado``, ``generic``) -> ``"git"`` (subprocess + GitCache)
    """
    if host_kind in ("github", "ghe_cloud", "ghes"):
        return "github"
    if host_kind == "gitlab":
        return "gitlab"
    return "git"


# SCP-like SSH form: ``user@host:path``. The path component does not need
# to start with a slash (that is what makes it SCP-like). Reuses the
# canonical regex from ``apm_cli.cache.url_normalize`` so SCP parsing here
# stays consistent with ``DependencyReference`` and policy discovery.
from apm_cli.cache.url_normalize import SCP_LIKE_RE as _SCP_LIKE_RE  # noqa: E402


def _looks_like_local_marketplace_source(raw: str) -> bool:
    """Heuristic match for local-path marketplace sources.

    Matches: absolute paths (``/...``), explicit relative (``./...``,
    ``../...``), home (``~``, ``~/...``), ``file://`` URIs, and Windows
    drive letters (``C:\\`` or ``C:/``). The leading-slash check is
    POSIX-only; on Windows, absolute paths arrive as drive-letter form.
    """
    if not raw:
        return False
    if raw.lower().startswith("file://"):
        return True
    if raw.startswith(("/", "./", "../", "~/", ".\\", "..\\", "~\\")) or raw == "~":
        return True
    # Windows drive letter: C:\foo or C:/foo
    return len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":" and raw[2] in ("\\", "/")


def _expand_local_path(raw: str) -> str:
    """Expand ``~`` and normalise to an absolute filesystem path string.

    Used when synthesising the ``file://`` URL stored in ``marketplaces.json``
    for local-kind entries. The result is *not* resolved (no symlink follow)
    because the fetcher does its own ``ensure_path_within`` guard against
    the post-``resolve`` location.
    """
    import os.path as _osp

    return _osp.abspath(_osp.expanduser(raw))


# ---------------------------------------------------------------------------
# Re-exports from siblings (Rule A: keep names patchable on this module)
# ---------------------------------------------------------------------------

from ._registry_cmds import (  # noqa: E402
    _check_gitignore_for_marketplace_json as _check_gitignore_for_marketplace_json,
)
from ._registry_cmds import (  # noqa: E402
    _default_alias_from_remote_url as _default_alias_from_remote_url,
)
from ._registry_cmds import _default_alias_from_url as _default_alias_from_url  # noqa: E402
from ._registry_cmds import (  # noqa: E402
    _display_source_kind as _display_source_kind,
)
from ._registry_cmds import (  # noqa: E402
    _is_remote_marketplace_json_url as _is_remote_marketplace_json_url,
)
from ._registry_cmds import (  # noqa: E402
    _local_source_points_to_file as _local_source_points_to_file,
)
from ._registry_cmds import (  # noqa: E402
    _marketplace_add_unsupported_host_error as _marketplace_add_unsupported_host_error,
)
from ._registry_cmds import (  # noqa: E402
    _should_warn_unpinned_git_url as _should_warn_unpinned_git_url,
)
from ._registry_cmds import (  # noqa: E402
    _split_source_fragment_ref as _split_source_fragment_ref,
)
from ._registry_cmds import add as add  # noqa: E402
from ._registry_cmds import browse as browse  # noqa: E402
from ._registry_cmds import list_cmd as list_cmd  # noqa: E402
from ._registry_cmds import remove as remove  # noqa: E402
from ._registry_cmds import update as update  # noqa: E402
from ._search_cmd import search as search  # noqa: E402
from ._table_ops import _CheckResult as _CheckResult  # noqa: E402
from ._table_ops import _DoctorCheck as _DoctorCheck  # noqa: E402
from ._table_ops import _extract_tag_versions as _extract_tag_versions  # noqa: E402
from ._table_ops import _load_current_versions as _load_current_versions  # noqa: E402
from ._table_ops import _OutdatedRow as _OutdatedRow  # noqa: E402
from ._table_ops import _render_build_error as _render_build_error  # noqa: E402
from ._table_ops import _render_build_table as _render_build_table  # noqa: E402
from ._table_ops import _render_check_table as _render_check_table  # noqa: E402
from ._table_ops import _render_doctor_table as _render_doctor_table  # noqa: E402
from ._table_ops import _render_outdated_table as _render_outdated_table  # noqa: E402
from .audit import audit  # noqa: E402
from .check import check  # noqa: E402
from .init import init  # noqa: E402
from .migrate import migrate  # noqa: E402
from .outdated import outdated  # noqa: E402
from .validate import validate  # noqa: E402

# Public surface: the click group + per-command callables. Domain types are
# re-exported from canonical sources for backward compatibility with tests
# and external consumers that patch via this package path. Submodules import
# their domain types from the canonical sources directly, not from here.
__all__ = [
    "BuildError",
    "BuildOptions",
    "BuildReport",
    "ConfigSource",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "MarketplaceBuilder",
    "MarketplaceGroup",
    "MarketplaceNotFoundError",
    "MarketplaceYmlError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "PathTraversalError",
    "RefNotFoundError",
    "RefResolver",
    "RemoteRef",
    "ResolvedPackage",
    "SemVer",
    "_CheckResult",
    "_DoctorCheck",
    "_OutdatedRow",
    "_check_gitignore_for_marketplace_json",
    "_default_alias_from_url",
    "_extract_tag_versions",
    "_find_duplicate_names",
    "_is_valid_alias",
    "_load_config_or_exit",
    "_load_current_versions",
    "_load_yml_or_exit",
    "_marketplace_add_unsupported_host_error",
    "_parse_marketplace_repo",
    "_parse_marketplace_source",
    "_render_build_error",
    "_render_build_table",
    "_render_check_table",
    "_render_doctor_table",
    "_render_outdated_table",
    "_warn_duplicate_names",
    "add",
    "audit",
    "browse",
    "check",
    "detect_config_source",
    "init",
    "list_cmd",
    "load_marketplace_config",
    "load_marketplace_yml",
    "marketplace",
    "migrate",
    "migrate_marketplace_yml",
    "outdated",
    "package",
    "parse_semver",
    "remove",
    "satisfies_range",
    "search",
    "translate_git_stderr",
    "update",
    "validate",
    "validate_path_segments",
]
