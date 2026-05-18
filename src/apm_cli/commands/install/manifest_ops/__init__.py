# pylint: disable=duplicate-code
"""APM install command and dependency installation engine."""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.gitlab_resolver import (
    _try_resolve_gitlab_direct_shorthand as _try_resolve_gitlab_direct_shorthand,
)
from apm_cli.install.insecure_policy import (
    _allow_insecure_host_callback as _allow_insecure_host_callback,
)
from apm_cli.install.insecure_policy import (
    _check_insecure_dependencies as _check_insecure_dependencies,
)
from apm_cli.install.insecure_policy import (
    _collect_insecure_dependency_infos as _collect_insecure_dependency_infos,
)
from apm_cli.install.insecure_policy import (
    _format_insecure_dependency_requirements,
)
from apm_cli.install.insecure_policy import (
    _format_insecure_dependency_warning as _format_insecure_dependency_warning,
)
from apm_cli.install.insecure_policy import (
    _get_insecure_dependency_url as _get_insecure_dependency_url,
)
from apm_cli.install.insecure_policy import (
    _guard_transitive_insecure_dependencies as _guard_transitive_insecure_dependencies,
)
from apm_cli.install.insecure_policy import (
    _InsecureDependencyInfo as _InsecureDependencyInfo,
)
from apm_cli.install.mcp.entry import _build_mcp_entry as _build_mcp_entry
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml as _add_mcp_to_apm_yml
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    dependency_reference_to_yaml_entry,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
    resolve_parsed_dependency_reference,
    user_scope_rejection_reason,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.phases.local_content import (
    _copy_local_package as _copy_local_package,
)
from apm_cli.install.phases.local_content import (
    _has_local_apm_content as _has_local_apm_content,
)
from apm_cli.install.phases.local_content import (
    _project_has_root_primitives,
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed
from apm_cli.install.services import (
    _integrate_local_content as _integrate_local_content,
)
from apm_cli.install.services import (
    _integrate_package_primitives as _integrate_package_primitives,
)
from apm_cli.install.validation import (
    _local_path_failure_reason as _local_path_failure_reason,
)
from apm_cli.install.validation import (
    _local_path_no_markers_hint as _local_path_no_markers_hint,
)
from apm_cli.install.validation import (
    _validate_package_exists as _validate_package_exists,
)
from apm_cli.utils.diagnostics import DiagnosticCollector as DiagnosticCollector

from ....constants import (
    APM_YML_FILENAME,
)
from ....core.command_logger import InstallLogger, _ValidationOutcome

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ....utils.console import _rich_error as _rich_error
from ....utils.console import _rich_success as _rich_success

# Re-export complex function from helper module
from .package_resolver import _resolve_package_references, _ResolvePackageReferencesRequest

# ---------------------------------------------------------------------------
# Manifest snapshot + rollback (W2-pkg-rollback, #827)
# ---------------------------------------------------------------------------
# When the user runs ``apm install <pkg>``, ``_validate_and_add_packages_to_apm_yml``
# mutates ``apm.yml`` BEFORE the install pipeline runs.  If the pipeline fails
# (policy block, download error, etc.) the failed package would stay in
# ``apm.yml`` forever.  These helpers snapshot the raw bytes before mutation
# and atomically restore on failure.
# ---------------------------------------------------------------------------


# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict

# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ....deps.apm_resolver import APMDependencyResolver
    from ....deps.github_downloader import GitHubPackageDownloader as GitHubPackageDownloader
    from ....deps.lockfile import LockFile as LockFile
    from ....deps.lockfile import get_lockfile_path as get_lockfile_path
    from ....deps.lockfile import migrate_lockfile_if_needed as migrate_lockfile_if_needed
    from ....integration import AgentIntegrator as AgentIntegrator
    from ....integration import PromptIntegrator as PromptIntegrator
    from ....integration.mcp_integrator import MCPIntegrator as MCPIntegrator
    from ....models.apm_package import APMPackage as APMPackage
    from ....models.apm_package import DependencyReference as DependencyReference

    class _ScopedInstallDependencyResolver(APMDependencyResolver):
        """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

        def __init__(self, *args, install_scope=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._install_scope = install_scope

        def expand_parent_repo_decl(self, parent_dep, child_dep):
            from ....core.scope import InstallScope

            if self._install_scope is InstallScope.USER:
                raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
            return super().expand_parent_repo_decl(parent_dep, child_dep)

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)
    _ScopedInstallDependencyResolver = None  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Package validation helpers (extracted from _validate_and_add_packages_to_apm_yml)


def _restore_manifest_from_snapshot(
    snapshot_path,
    snapshot_bytes,
    logger=None,
):
    """Atomically restore ``apm.yml`` to its pre-mutated bytes."""
    del logger
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    snapshot_path.write_bytes(snapshot_bytes)


def _maybe_rollback_manifest(apm_yml_path, snapshot_bytes, logger=None):
    """Best-effort manifest restore used by install failure handlers."""
    if snapshot_bytes is None:
        return
    try:
        _restore_manifest_from_snapshot(apm_yml_path, snapshot_bytes, logger=logger)
        if logger:
            logger.progress("apm.yml restored to its previous state.")
    except Exception as err:
        if logger:
            logger.warning(f"Failed to restore apm.yml: {err}")


def _check_package_conflicts(current_deps):
    """Build set of already-installed package identities.

    *current_deps* is the raw ``dependencies.apm`` list from ``apm.yml``.
    """
    # Detect duplicates against existing deps
    existing_identities: set[str] = set()
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, dict):
                dep_ref = DependencyReference.parse_from_dict(dep_entry)
            elif isinstance(dep_entry, str):
                dep_ref = DependencyReference.parse(dep_entry)
            else:
                continue
            existing_identities.add(dep_ref.get_identity())
        except Exception:
            pass
    return existing_identities


from ._manifest_io import (
    _load_apm_yml_data,
    _log_dry_run_additions,
    _merge_packages_into_yml,
    _MergeYmlContext,
    _ValidationAddRequest,
)


def _validate_and_add_packages_to_apm_yml(
    packages,
    request: _ValidationAddRequest | None = None,
    **legacy_kwargs,
):
    """Validate packages exist and can be accessed, then add to apm.yml dependencies section.

    Implements normalize-on-write: any input form (HTTPS URL, SSH URL, FQDN, shorthand)
    is canonicalized before storage. Default host (github.com) is stripped;
    non-default hosts are preserved. Duplicates are detected by identity.

    Args:
        packages: Package specifiers to validate and add.
        dry_run: If True, only show what would be added.
        dev: If True, write to devDependencies instead of dependencies.
        logger: InstallLogger for structured output.
        manifest_path: Explicit path to apm.yml (defaults to cwd/apm.yml).
        auth_resolver: Shared auth resolver for caching credentials.
        scope: InstallScope controlling project vs user deployment.

    Returns:
        Tuple of (validated_packages list, _ValidationOutcome).
    """
    from pathlib import Path

    request = request or _ValidationAddRequest(**legacy_kwargs)
    dry_run = request.dry_run
    logger = request.logger
    apm_yml_path = request.manifest_path or Path(APM_YML_FILENAME)

    # Read current apm.yml
    data = _load_apm_yml_data(apm_yml_path, logger)

    # Ensure dependencies structure exists
    dep_section = "devDependencies" if request.dev else "dependencies"
    if dep_section not in data:
        data[dep_section] = {}
    if "apm" not in data[dep_section]:
        data[dep_section]["apm"] = []

    current_deps = data[dep_section]["apm"] or []

    # Detect duplicates against existing deps
    existing_identities = _check_package_conflicts(current_deps)

    # Validate and canonicalize all package references
    (
        valid_outcomes,
        invalid_outcomes,
        validated_packages,
        _marketplace_provenance,
        _apm_yml_entries,
        dependencies_changed,
    ) = _resolve_package_references(
        packages,
        current_deps,
        existing_identities,
        _ResolvePackageReferencesRequest(
            auth_resolver=request.auth_resolver,
            logger=logger,
            scope=request.scope,
            allow_insecure=request.allow_insecure,
        ),
    )

    outcome = _ValidationOutcome(
        valid=valid_outcomes,
        invalid=invalid_outcomes,
        marketplace_provenance=_marketplace_provenance or None,
    )

    # Let the logger emit a summary and decide whether to continue
    if logger:
        should_continue = logger.validation_summary(outcome)
        if not should_continue:
            return [], outcome

    if not validated_packages:
        if dry_run:
            if logger:
                logger.progress("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them
        persist_dependency_list_if_changed(
            dependencies_changed=dependencies_changed,
            data=data,
            dep_section=dep_section,
            current_deps=current_deps,
            apm_yml_path=apm_yml_path,
            apm_yml_filename=APM_YML_FILENAME,
            logger=logger,
            rich_error=_rich_error,
            sys_exit=sys.exit,
        )
        return [], outcome

    if dry_run:
        _log_dry_run_additions(validated_packages, logger)
        return validated_packages, outcome

    # Persist validated packages to apm.yml
    _merge_packages_into_yml(
        validated_packages,
        _MergeYmlContext(
            apm_yml_entries=_apm_yml_entries,
            current_deps=current_deps,
            data=data,
            dep_section=dep_section,
            apm_yml_path=apm_yml_path,
            dev=request.dev,
            logger=logger,
        ),
    )

    return validated_packages, outcome


__all__ = [
    "_check_package_conflicts",
    "_maybe_rollback_manifest",
    "_merge_packages_into_yml",
    "_resolve_package_references",
    "_restore_manifest_from_snapshot",
    "_validate_and_add_packages_to_apm_yml",
]
