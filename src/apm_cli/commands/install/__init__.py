from __future__ import annotations

from ._command_context import InstallContext  # noqa: F401
from .argv_split import _get_invocation_argv, _split_argv_at_double_dash  # noqa: F401
from .cli import install  # noqa: F401
from .flags import InstallDependencyParams, MCPInvokeParams  # noqa: F401
from .manifest_ops import (  # noqa: F401
    _check_package_conflicts,
    _hash_deployed,
    _maybe_rollback_manifest,
    _merge_packages_into_yml,
    _resolve_package_references,
    _restore_manifest_from_snapshot,
    _validate_and_add_packages_to_apm_yml,
)
from .mcp_flow import _handle_mcp_install  # noqa: F401
from .pipeline import _install_apm_dependencies, _install_apm_packages  # noqa: F401
from .scanning import _pre_deploy_security_scan  # noqa: F401
from .summary import _post_install_summary  # noqa: F401

CommandInstallContext = InstallContext

from .manifest_ops import (  # noqa: F401
    APM_DEPS_AVAILABLE,
    APMPackage,
    DependencyReference,
    DiagnosticCollector,
    InstallLogger,
    LockFile,
    MCPIntegrator,
    Path,
    _add_mcp_to_apm_yml,
    _allow_insecure_host_callback,
    _build_mcp_entry,
    _collect_insecure_dependency_infos,
    _copy_local_package,
    _format_insecure_dependency_warning,
    _get_insecure_dependency_url,
    _guard_transitive_insecure_dependencies,
    _has_local_apm_content,
    _InsecureDependencyInfo,
    _integrate_local_content,
    _integrate_package_primitives,
    _local_path_failure_reason,
    _local_path_no_markers_hint,
    _rich_success,
    _try_resolve_gitlab_direct_shorthand,
    _validate_package_exists,
    get_lockfile_path,
    migrate_lockfile_if_needed,
)
from .mcp_flow import _run_mcp_install  # noqa: F401
from .pipeline import _APM_IMPORT_ERROR, _check_insecure_dependencies  # noqa: F401
