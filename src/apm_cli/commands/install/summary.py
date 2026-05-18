# pylint: disable=duplicate-code
"""APM install command and dependency installation engine."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
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


# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.

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
    from ...deps.github_downloader import GitHubPackageDownloader as GitHubPackageDownloader
    from ...deps.lockfile import LockFile as LockFile
    from ...deps.lockfile import get_lockfile_path as get_lockfile_path
    from ...deps.lockfile import migrate_lockfile_if_needed as migrate_lockfile_if_needed
    from ...integration import AgentIntegrator as AgentIntegrator
    from ...integration import PromptIntegrator as PromptIntegrator
    from ...integration.mcp_integrator import MCPIntegrator as MCPIntegrator
    from ...models.apm_package import APMPackage as APMPackage
    from ...models.apm_package import DependencyReference as DependencyReference

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


@dataclass(frozen=True, slots=True)
class _LegacyPostInstallSummaryParams:
    """Compatibility wrapper for legacy summary keyword arguments."""

    apm_count: int
    mcp_count: int
    apm_diagnostics: object
    force: bool
    elapsed_seconds: float | None = None


def _post_install_summary(*, logger, params=None, **legacy_kwargs):
    """Thin shim forwarding to :func:`apm_cli.install.summary.render_post_install_summary`.

    Kept as a module-level alias so existing tests that
    ``@patch("apm_cli.commands.install._post_install_summary")`` continue
    to work after the extraction (microsoft/apm#1116, F5).
    """
    from apm_cli.install.summary import PostInstallSummaryParams, render_post_install_summary

    if params is None:
        legacy_params = _LegacyPostInstallSummaryParams(**legacy_kwargs)
        params = PostInstallSummaryParams(
            apm_count=legacy_params.apm_count,
            mcp_count=legacy_params.mcp_count,
            apm_diagnostics=legacy_params.apm_diagnostics,
            force=legacy_params.force,
            elapsed_seconds=legacy_params.elapsed_seconds,
        )

    render_post_install_summary(logger, params)
