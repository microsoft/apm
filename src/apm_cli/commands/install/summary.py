"""APM install command and dependency installation engine."""

import builtins
import contextlib
import dataclasses
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

if TYPE_CHECKING:
    from apm_cli.install.plan import UpdatePlan

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan  # noqa: F401
from apm_cli.install.insecure_policy import (
    InsecureDependencyPolicyError,
    _allow_insecure_host_callback,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,  # noqa: F401
    _format_insecure_dependency_requirements,
    _format_insecure_dependency_warning,  # noqa: F401
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,  # noqa: F401
    _InsecureDependencyInfo,  # noqa: F401
)

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.mcp.entry import _build_mcp_entry  # noqa: F401
from apm_cli.install.mcp.writer import _add_mcp_to_apm_yml  # noqa: F401
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
from apm_cli.install.phases.local_content import (
    _copy_local_package,  # noqa: F401
    _has_local_apm_content,  # noqa: F401
    _project_has_root_primitives,
)

# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed  # noqa: F401

# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.
from apm_cli.install.services import (
    _integrate_local_content,  # noqa: F401
    _integrate_package_primitives,  # noqa: F401
)

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,  # noqa: F401
    _validate_package_exists,
)
from apm_cli.utils.diagnostics import DiagnosticCollector  # noqa: F401

from ...constants import (
    APM_YML_FILENAME,
    InstallMode,
)
from ...core.auth import AuthResolver
from ...core.command_logger import InstallLogger, _ValidationOutcome
from ...core.target_detection import TargetParamType

# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.
from ...install.mcp.command import run_mcp_install as _run_mcp_install
from ...install.mcp.conflicts import (
    validate_mcp_conflicts as _validate_mcp_conflicts,
)
from ...install.mcp.registry import (
    resolve_registry_url as _resolve_registry_url,
)
from ...install.mcp.registry import (
    validate_mcp_dry_run_entry as _validate_mcp_dry_run_entry,
)
from ...install.mcp.registry import (
    validate_registry_url as _validate_registry_url,
)
from ...utils.console import _rich_echo, _rich_error, _rich_info, _rich_success  # noqa: F401
from .._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _update_gitignore_for_apm_modules,  # noqa: F401
)

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
    from ...deps.apm_resolver import APMDependencyResolver
    from ...deps.github_downloader import GitHubPackageDownloader  # noqa: F401
    from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ...integration import AgentIntegrator, PromptIntegrator  # noqa: F401
    from ...integration.mcp_integrator import MCPIntegrator
    from ...models.apm_package import APMPackage, DependencyReference

    class _ScopedInstallDependencyResolver(APMDependencyResolver):
        """Install-time resolver; blocks ``git: parent`` expansion at user scope."""

        def __init__(self, *args, install_scope=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._install_scope = install_scope

        def expand_parent_repo_decl(self, parent_dep, child_dep):
            from ...core.scope import InstallScope

            if self._install_scope is InstallScope.USER:
                raise ValueError(GIT_PARENT_USER_SCOPE_ERROR)
            return super().expand_parent_repo_decl(parent_dep, child_dep)

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)
    _ScopedInstallDependencyResolver = None  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Package validation helpers (extracted from _validate_and_add_packages_to_apm_yml)

from ._command_context import InstallContext


def _post_install_summary(
    *, logger, apm_count, mcp_count, apm_diagnostics, force, elapsed_seconds=None
):
    """Thin shim forwarding to :func:`apm_cli.install.summary.render_post_install_summary`.

    Kept as a module-level alias so existing tests that
    ``@patch("apm_cli.commands.install._post_install_summary")`` continue
    to work after the extraction (microsoft/apm#1116, F5).
    """
    from apm_cli.install.summary import render_post_install_summary

    render_post_install_summary(
        logger=logger,
        apm_count=apm_count,
        mcp_count=mcp_count,
        apm_diagnostics=apm_diagnostics,
        force=force,
        elapsed_seconds=elapsed_seconds,
    )
