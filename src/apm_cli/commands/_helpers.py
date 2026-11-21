"""Shared CLI helpers for APM command modules.

This module must NOT import from any command module.
"""

import builtins
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import click
from colorama import Fore, Style
from colorama import init as colorama_init

from apm_cli.core.scope import manifest_file

from ..agent_plugins.validation import is_valid_plugin_name as _is_valid_agent_plugin_name
from ..constants import (
    APM_MODULES_DIR,
    APM_YML_FILENAME,
    SKILL_MD_FILENAME,
)
from ..core import project_name as _project_name

# Re-exported under their historical names; the owners now live below the
# commands layer so install/ can use them without importing commands (#3879).
from ..install.helpers.gitignore import (  # noqa: F401
    update_gitignore_for_apm_modules as _update_gitignore_for_apm_modules,
)
from ..models.manifest_bootstrap import (  # noqa: F401
    auto_detect_description as _auto_detect_description,
)
from ..models.manifest_bootstrap import bootstrap_config as _get_bootstrap_config
from ..models.manifest_bootstrap import (  # noqa: F401
    write_minimal_manifest as _create_minimal_apm_yml,
)
from ..update_policy import get_update_hint_message, is_self_update_enabled
from ..utils.atomic_io import (
    atomic_write_text as _atomic_write,  # noqa: F401 -- re-exported; tests import from apm_cli.commands._helpers
)
from ..utils.console import _rich_echo, _rich_info, _rich_warning
from ..utils.git_env import get_git_executable
from ..utils.path_security import PathTraversalError, validate_path_segments
from ..utils.themed_console import _get_console, _rich_blank_line  # noqa: F401
from ..utils.version_checker import check_for_updates
from ..version import get_version
from ..version_output import print_version  # noqa: F401 -- compatibility re-export
from .deps._utils import _scan_installed_packages

# Thin compatibility aliases only; core/project_name.py remains the AC18 owner.
_resolve_bootstrap_project_name = _project_name.resolve_bootstrap_project_name
_validate_project_name = _project_name.validate_project_name

# CRITICAL: Shadow Click commands at module level to prevent namespace collision
# When Click commands like 'config set' are defined, calling set() can invoke the command
# instead of the Python built-in. This affects ALL functions in this module.
set = builtins.set
list = builtins.list
dict = builtins.dict

# Initialize colorama for fallback
colorama_init(autoreset=True)

# Legacy colorama constants for compatibility
TITLE = f"{Fore.CYAN}{Style.BRIGHT}"
SUCCESS = f"{Fore.GREEN}{Style.BRIGHT}"
ERROR = f"{Fore.RED}{Style.BRIGHT}"
INFO = f"{Fore.BLUE}"
WARNING = f"{Fore.YELLOW}"
HIGHLIGHT = f"{Fore.MAGENTA}{Style.BRIGHT}"
RESET = Style.RESET_ALL


# -------------------------------------------------------------------
# TTY detection
# -------------------------------------------------------------------


def _is_interactive():
    """Return True when both stdin and stdout are attached to a TTY."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _lazy_yaml():
    """Lazy import for yaml module to improve startup performance."""
    try:
        import yaml

        return yaml
    except ImportError:
        raise ImportError("PyYAML is required but not installed")  # noqa: B904


def _lazy_prompt():
    """Lazy import for Rich Prompt to improve startup performance."""
    try:
        from rich.prompt import Prompt

        return Prompt
    except ImportError:
        return None


def _lazy_confirm():
    """Lazy import for Rich Confirm to improve startup performance."""
    try:
        from rich.prompt import Confirm

        return Confirm
    except ImportError:
        return None


# ------------------------------------------------------------------
# Shared orphan-detection helpers
# ------------------------------------------------------------------


def _expand_with_ancestors(
    paths: Iterable[str], installed: Iterable[str] | None = None
) -> set[str]:
    """Expand a set of expected paths to include ancestor prefixes.

    Given ``{"owner/repo/.apm/skills/my-skill"}``, returns a set containing
    the original path plus all intermediate path prefixes with 2+ segments
    (e.g., ``"owner/repo"``, ``"owner/repo/.apm"``,
    ``"owner/repo/.apm/skills"``, plus the original
    ``"owner/repo/.apm/skills/my-skill"``).
    This allows O(1) membership checks when determining whether a scanned
    directory is an ancestor of an expected package path.

    Ancestor expansion exists because a subdirectory dependency
    (``git: owner/repo, path: .apm/skills/x``) is installed by cloning the
    entire repo to ``apm_modules/owner/repo/``. Intermediate filesystem
    directories created by that clone are required parts of the install --
    not stale leftovers.

    Real-orphan safety: when *installed* is supplied, an ancestor that
    matches one of the installed paths is NOT added to the expansion
    unless that path is also directly declared in *paths*. Callers should
    pass only the subset of installed paths that look like *real
    standalone packages* (i.e., directories that ship their own
    ``apm.yml`` or ``SKILL.md``) -- not filesystem intermediaries (which typically have
    only a ``.apm/`` subtree from a cloned subdir dep). This preserves
    orphan detection for the case where a user has a genuinely orphaned
    ``owner/repo`` package on disk alongside a declared sibling
    subdirectory dep (``owner/repo/.apm/skills/foo``): only filesystem
    intermediaries are suppressed, never real installed packages.

    Security contract -- ancestor depth cap: ``get_install_path()``
    anchors installs at the 2-segment repo root (GitHub) or 3-segment
    root (ADO). Anything deeper is a filesystem-intermediary path
    (``.apm/``, ``skills/``, ...) that ``_scan_installed_packages``
    skips, so emitting ancestors past depth 3 would only widen the
    orphan-suppression surface without serving any real lookup. The
    loop is therefore capped at depth 3 (``min(4, len(parts))``), which
    bounds the number of paths an attacker-influenced ``apm.yml`` dep
    declaration can hide from orphan detection. If the install strategy
    ever grows deeper roots, lift this cap and document the new
    invariant here.

    Traversal guard: any input path that fails
    :func:`apm_cli.utils.path_security.validate_path_segments` (which
    rejects both ``.`` and ``..`` segments after backslash
    normalisation) is kept in the result as-is (membership check) but
    produces no ancestors. Routing through the canonical guard --
    rather than a hand-rolled ``".." in parts`` check -- ensures
    single-dot segments (``owner/./repo``) are also caught and keeps
    the project's path-validation contract centralised.
    """
    materialized = list(paths)
    materialized_set = set(materialized)
    expanded = set(materialized)
    installed_set = set(installed) if installed is not None else set()
    for p in materialized:
        try:
            validate_path_segments(p, context="ancestor expansion")
        except PathTraversalError:
            continue
        # Normalise backslashes so Windows-style tokens split into the
        # same parts as POSIX inputs for the depth-capped loop below.
        normalised = p.replace("\\", "/")
        parts = normalised.split("/")
        # Cap at depth 3 -- the ADO install-root depth -- to bound the
        # ancestor-suppression surface (see security contract above).
        for i in range(2, min(4, len(parts))):
            ancestor = "/".join(parts[:i])
            # Do not mask a real installed package via ancestor expansion;
            # only filesystem intermediaries should be added. A real
            # installed package that is also directly declared remains in
            # expanded via materialized_set.
            if ancestor in installed_set and ancestor not in materialized_set:
                continue
            expanded.add(ancestor)
    return expanded


def _standalone_installed_packages(
    installed: Iterable[str], apm_modules_dir: Path, lockfile=None
) -> list:
    """Filter *installed* to entries that look like real standalone packages.

    Determination order (tamper-evident first):

    1. Path appears as a dependency key in *lockfile* -- the canonical
       record of what APM installed. The lockfile is integrity-checked
       and not forgeable by dropping/omitting files in ``apm_modules/``.
    2. Fallback: path has its own ``apm.yml`` or ``SKILL.md`` file. Used when the lockfile
       is absent (older installs / fresh checkouts) or does not list
       the key. A directory with only a ``.apm/`` marker is treated as
       a filesystem intermediary, not a standalone package.

    Package markers preserve standalone orphan detection even when a
    declaration points at a subdirectory of a removed package root.

    Failure mode: only narrowly-typed shape errors against
    ``lockfile.dependencies`` (``AttributeError`` / ``TypeError`` /
    ``KeyError``) are absorbed and degrade to the package-marker
    fallback. Any other exception (e.g. lockfile parse / I/O failure)
    propagates so the outer caller can decide whether to log or fail
    closed -- preventing a corrupted or attacker-crafted lockfile from
    silently disabling the tamper-evident standalone check.
    """
    lockfile_keys: set[str] = set()
    if lockfile is not None:
        try:
            for dep_key in lockfile.dependencies:
                if dep_key:
                    lockfile_keys.add(dep_key)
        except (AttributeError, TypeError, KeyError):
            lockfile_keys = set()
    standalone: list = []
    for p in installed:
        if p in lockfile_keys:
            standalone.append(p)
            continue
        if (
            manifest_file(apm_modules_dir / p).exists()
            or (apm_modules_dir / p / SKILL_MD_FILENAME).is_file()
        ):
            standalone.append(p)
    return standalone


def _check_orphaned_packages():
    """Check for packages in apm_modules/ that are not declared in apm.yml or apm.lock.

    Considers both direct dependencies (from apm.yml) and transitive dependencies
    (from apm.lock) as expected packages, so transitive deps are not falsely
    flagged as orphaned.

    Returns:
        List[str]: List of orphaned package names in org/repo or org/project/repo format
    """
    try:
        if not manifest_file(Path(".")).exists():
            return []

        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            return []

        try:
            from ..deps.lockfile import LockFile, get_lockfile_path
            from ..models.apm_package import APMPackage

            apm_package = APMPackage.from_apm_yml(manifest_file(Path(".")))
            declared_deps = apm_package.get_all_apm_dependencies()
            lockfile = LockFile.read(get_lockfile_path(Path.cwd()))
        except Exception:
            return []

        installed = _scan_installed_packages(apm_modules_dir)
        return list(
            _inspect_installed_orphans(
                declared_deps, lockfile, apm_modules_dir, installed
            ).removed_paths
        )
    except Exception:
        return []


def _inspect_installed_orphans(declared_deps, lockfile, apm_modules_dir: Path, installed):
    """Ask the orphan owner which installed roots the retained graph no longer needs."""
    from ..install.orphans import (
        InstalledPaths,
        OrphanOperation,
        ReachabilityProbe,
        decide_orphan_removal,
    )

    return decide_orphan_removal(
        operation=OrphanOperation.INSPECT,
        roots=(),
        candidate_universe=lockfile,
        installed_paths=InstalledPaths(root=apm_modules_dir, packages=frozenset(installed)),
        resolved_keys=None,
        intended=declared_deps,
        materialized=None,
        reachability=ReachabilityProbe(
            project_root=apm_modules_dir.parent,
            apm_modules_dir=apm_modules_dir,
            direct_refs=tuple(declared_deps),
            dev_refs=(),
        ),
        deployment_claims=None,
    )


# ------------------------------------------------------------------
# Dependency-update helpers (shared by `apm update` and `apm deps update`)
# ------------------------------------------------------------------


class UnknownPackageError(Exception):
    """A requested package token matched no declared dependency.

    Carries the offending *token* and the list of *available* display
    names so the CLI caller can render a consistent error message.
    """

    def __init__(self, token: str, available: list[str]):
        self.token = token
        self.available = available
        super().__init__(f"Package '{token}' not found")


def resolve_requested_packages(
    packages: tuple[str, ...] | list[str],
    all_deps: list,
) -> list[str] | None:
    """Map requested package tokens to canonical dependency keys.

    The install engine matches ``only_packages`` by ``DependencyReference``
    identity (e.g. ``owner/repo``), so short names like ``compliance-rules``
    must be normalised to their canonical key before the engine is called.

    Args:
        packages: Raw tokens from the CLI ``[PACKAGES]...`` argument.
        all_deps: Declared dependencies (direct + dev) to match against.

    Returns:
        An order-preserving, de-duplicated list of canonical keys, or
        ``None`` when *packages* is empty (meaning "refresh everything").

    Raises:
        UnknownPackageError: when a token matches no declared dependency.
    """
    if not packages:
        return None

    token_to_canonical: dict[str, str] = {}
    for dep in all_deps:
        canonical_key = dep.get_unique_key() or dep.repo_url or dep.get_display_name()
        tokens = {canonical_key, dep.get_display_name(), dep.repo_url}
        if getattr(dep, "alias", None):
            tokens.add(dep.alias)
        parts = dep.repo_url.split("/")
        if len(parts) >= 2:
            tokens.add(parts[-1])
        for token in tokens:
            if token and token not in token_to_canonical:
                token_to_canonical[token] = canonical_key

    resolved: list[str] = []
    seen: set[str] = set()
    for pkg in packages:
        canonical = token_to_canonical.get(pkg)
        if not canonical:
            available = [dep.get_display_name() for dep in all_deps]
            raise UnknownPackageError(pkg, available)
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    return resolved


def _check_and_notify_updates():
    """Check for updates and notify user non-blockingly."""
    try:
        # Skip notifications when self-update is disabled by distribution policy.
        if not is_self_update_enabled():
            return

        # Skip version check in E2E test mode to avoid interfering with tests
        if os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes"):
            return

        from ..deps.egress_route import EgressSurface, is_refused

        if is_refused(EgressSurface.VERSION_CHECK):
            context = click.get_current_context(silent=True)
            if context is not None and (context.obj or {}).get("verbose"):
                from ..core.remediation import CLI_UPDATE_CHECK_SKIPPED

                skipped = "Update check skipped: PROXY_REGISTRY_ONLY is set"
                _rich_info(
                    " ".join((skipped, f"({CLI_UPDATE_CHECK_SKIPPED.clause()})")),
                    symbol="info",
                )
            return

        current_version = get_version()

        # Skip check for development versions
        if current_version == "unknown":
            return

        latest_version = check_for_updates(current_version)

        if latest_version:
            # Display yellow warning with update command
            _rich_warning(
                f"A new version of APM is available: {latest_version} (current: {current_version})",
                symbol="warning",
            )

            # Show update command using helper for consistency
            _rich_echo(get_update_hint_message(), color="yellow", bold=True)

            # Add a blank line for visual separation
            _rich_echo("")
    except Exception:
        # Silently fail - version checking should never block CLI usage
        pass


# ------------------------------------------------------------------
# Script / config helpers (shared by run, list, config commands)
# ------------------------------------------------------------------


def _load_apm_config():
    """Load configuration from apm.yml."""
    if manifest_file(Path(".")).exists():
        from ..models.manifest_io import load_manifest

        return load_manifest(APM_YML_FILENAME)
    return None


def _get_default_script():
    """Get the default script (start) from apm.yml scripts."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config and "start" in apm_config["scripts"]:
        return "start"
    return None


def _list_available_scripts():
    """List all available scripts from apm.yml."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config:
        return apm_config["scripts"]
    return {}


# ------------------------------------------------------------------
# Init helpers (shared by init and install commands)
# ------------------------------------------------------------------


def _auto_detect_author():
    """Auto-detect author from git config."""
    import subprocess

    try:
        result = subprocess.run(
            [get_git_executable(), "config", "user.name"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "Developer"


def _get_default_config(project_name):
    """Get default configuration for new projects with auto-detection."""
    config = _get_bootstrap_config(project_name)
    config["author"] = _auto_detect_author()
    return config


def _validate_plugin_name(name):
    """Validate a plugin name through the Agent Plugins contract owner."""
    return _is_valid_agent_plugin_name(name)


def _create_plugin_json(config, mode: str = "agent", target_path: Path | None = None):
    """Create plugin.json file with package metadata.

    Args:
        config: dict with name, version, description, author keys.
        mode: "agent" to emit Agent Plugins v1 scaffold, or "claude" to
              preserve the legacy Claude-compatible scaffold.
        target_path: Optional output path. Defaults to ``plugin.json``.
    """
    import json

    # No license is scaffolded: the author must choose one deliberately.
    if mode == "claude":
        # Preserve the legacy Claude scaffold shape
        plugin_data = {
            "name": config["name"],
            "version": config.get("version", "0.1.0"),
            "description": config.get("description", ""),
            "author": {"name": config.get("author", "")},
        }
    else:
        # Agent Plugins v1 canonical scaffold
        from ..agent_plugins.constants import (
            COM_MICROSOFT_APM_NAMESPACE,
            COM_MICROSOFT_APM_SCHEMA_VERSION,
            PLUGIN_SCHEMA_ID,
        )

        plugin_data = {
            "$schema": PLUGIN_SCHEMA_ID,
            "name": config["name"],
            "version": config.get("version", "0.1.0"),
            "description": config.get("description", ""),
            "author": {"name": config.get("author", "")},
            "extensions": {
                COM_MICROSOFT_APM_NAMESPACE: {"schemaVersion": COM_MICROSOFT_APM_SCHEMA_VERSION}
            },
        }

    output_path = target_path or Path("plugin.json")
    output_path.write_text(json.dumps(plugin_data, indent=2) + "\n", encoding="utf-8")


def _find_apm_yml(start: Path | None = None) -> Path | None:
    """Walk parent directories from ``start`` (or cwd) to find ``apm.yml``.

    Matches the npm / cargo / poetry ergonomic: a developer running
    an ``apm`` command from a subdirectory of their project (``src/``,
    ``docs/``, ``scripts/``) finds the manifest and operates on it.

    The walk stops at the filesystem root or when an ``apm.yml`` is
    found, whichever comes first. Returns the absolute path to the
    ``apm.yml`` file when found; ``None`` when no project root is
    discoverable from ``start`` upward.
    """
    cwd = (start or Path.cwd()).resolve()
    for candidate in (cwd, *cwd.parents):
        manifest = manifest_file(candidate)
        if manifest.is_file():
            return manifest
    return None


# Shared by ``apm install`` and ``apm update`` so both commands expose the same
# per-invocation consent for transitive self-defined MCP servers.
trust_transitive_mcp_option = click.option(
    "--trust-transitive-mcp",
    is_flag=True,
    help=(
        "Trust MCP servers from transitive packages that are self-defined or set "
        "their own registry or configuration (skip re-declaration requirement)"
    ),
)
